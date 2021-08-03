#!/usr/bin/python3
import os
import re
import sys
import json
import boto3
import gitlab
import logging
import argparse
import subprocess as proc
from decouple import config
from logging import handlers
from datetime import datetime

APP_NAME = "git-mirror"


def make_parents(filename, dir=False):
    base = filename if dir else os.path.dirname(filename)
    if base and not os.path.exists(base):
        os.makedirs(base)


def get_logger(filename):
    make_parents(filename)

    log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s')

    logger = logging.getLogger(APP_NAME)

    fileHandler = handlers.TimedRotatingFileHandler(filename, when='D')
    fileHandler.setFormatter(log_formatter)
    logger.addHandler(fileHandler)

    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(log_formatter)
    logger.addHandler(consoleHandler)

    return logger


def raise_alert(message):
    pass


class RepoInfo:
    def __init__(self):
        self.repo_dir = None
        self.repo_name = None
        self.repo_path = None
        self.exists = False
        self.is_alias = False

        self.origin = None
        self.replicas = {}
        self.aliases = []


class App:
    def __init__(self, logger, dry_run):
        self.log = logger
        self.dry_run = dry_run

        if self.dry_run:
            self.log.info("starting in dry-run mode")

        self.codecommit_client = boto3.client("codecommit")
        self.gitlab_client = gitlab.Gitlab("https://gitlab.com",
                                           private_token=config("GITLAB_TOKEN"))

        self.gitlab_namespace = config("GITLAB_NAMESPACE")

    def run_command(self, cmd, *args, cwd=None):
        if self.dry_run:
            self.log.info(f"dry running command: {[cmd] + list(args)}")
            return {"stdout": "", "stderr": ""}, None

        try:
            if cwd is not None and len(cwd.strip()) == 0:
                cwd = "."

            exec = proc.run([cmd] + list(args),
                            cwd=cwd,
                            stdout=proc.PIPE,
                            stderr=proc.PIPE,
                            universal_newlines=True)
        except Exception as err:
            return {"stdout": "", "stderr": ""}, err

        err = None
        if exec.returncode != 0:
            err = Exception(f"[{cmd}] command exited with status {exec.returncode}")

        return {'stdout': exec.stdout, 'stderr': exec.stderr}, err

    def log_cmd_err(self, msg, output, err):
        self.log.error(f"{msg} due to err=[{err}]. stdout=[{output['stdout']}], stderr=[{output['stderr']}]")

    def ls_remote(self, repo, ref="HEAD"):
        output, err = app.run_command("git", "ls-remote", repo, ref)

        if err != None:
            self.log.debug(f"ls-remote failed with err=[{err}], output=[{output}]")
            return None

        match = re.match(r"([a-f0-9]{40})", output["stdout"])

        if not match:
            return ""  # empty repo (no commits)

        return match.group(0)

    def create_remote(self, url):
        repo_name = os.path.splitext(os.path.basename(url))[0]
        try:
            if "gitlab" in url:
                self.gitlab_client.projects.create({
                    "name": repo_name,
                    "namespace_id": self.gitlab_namespace
                })
            elif "codecommit" in url:
                self.codecommit_client.create_repository(repositoryName=repo_name)

            else:
                return Exception(f"Unknown replication server at [{url}]")

        except Exception as err:
            return err

    def set_alias_info(self, repo_dir, info):
        info.repo_dir = repo_dir
        info.repo_path = os.path.join(repo_dir, info.repo_name)
        info.exists = os.path.exists(info.repo_path)
        info.is_alias = False

        if info.exists:
            return

        for name in info.aliases:
            repo_path = os.path.join(repo_dir, name)

            if os.path.exists(repo_path):
                info.repo_name = name
                info.repo_path = repo_path
                info.is_alias = True
                info.exists = True

                return

    def clone_mirror(self, repo_info):
        start_time = datetime.now()

        self.log.info(f"cloning mirror repo [{repo_info.repo_name}] with origin=[{repo_info.origin}] into [{repo_info.repo_dir}]")

        output, err = self.run_command("git", "clone", "--mirror", repo_info.origin, repo_info.repo_name, cwd=repo_info.repo_dir)

        if err is not None:
            self.log_cmd_err(f"cannot clone mirror for [{repo_info.repo_name}]", output, err)
            return False

        duration = str(datetime.now() - start_time)
        self.log.info(f"cloned mirror repo [{repo_info.repo_name}] with origin=[{repo_info.origin}] into [{repo_info.repo_dir}]. Took [{duration}]")

        return True

    def add_replica(self, repo_info, replica_name, replica_url):
        output, err = self.run_command("git", "config", "--get", f"remote.{replica_name}.url", cwd=repo_info.repo_path)

        # Check if this replica with the same URL already exists
        if err is None:
            old_url = output["stdout"].split('\n')[0]
            if old_url == replica_url:
                self.log.debug(f"replica [{replica_name}] already exists in [{repo_info.repo_path}]")
                return True

            output, err = self.run_command("git", "remote", "set-url",
                                           replica_name,
                                           replica_url, cwd=repo_info.repo_path)

            if err is not None:
                self.log_cmd_err(f"cannot perform set-url on [{replica_name}]", output, err)
                return False

            self.log.debug(f"replica [{replica_name}] url updated from [{old_url}] to [{replica_url}]")
            return True

        output, err = self.run_command("git", "remote", "add", "--mirror",
                                       replica_name,
                                       replica_url, cwd=repo_info.repo_path)

        if err is not None:
            self.log_cmd_err(f"cannot add replica [{replica_name}] to [{repo_info.repo_path}]", output, err)
            return False

        self.log.info(f"added replica [{replica_name}:{replica_url}] to [{repo_info.repo_path}]")

        repo_info.replicas[replica_name] = replica_url

        return True

    def sync(self, repo_info):
        self.log.info(f"fetching [{repo_info.repo_path}] origin")

        start_time = datetime.now()
        output, err = self.run_command("git",  "fetch", "--prune", "origin", cwd=repo_info.repo_path)

        if err is not None:
            self.log_cmd_err(f"cannot fetch [{repo_info.repo_path}]", output, err)
            return 1

        self.log.info(f"fetched [{repo_info.repo_path}]. Took [{str(datetime.now()-start_time)}]")

        err_count = 0
        for replica in repo_info.replicas:
            self.log.info(f"pushing to [{replica}] replica of [{repo_info.repo_path}]")

            start_time = datetime.now()
            output, err = self.run_command("git", "push", "--mirror", replica, cwd=repo_info.repo_path)

            if err is not None:
                self.log_cmd_err(f"cannot push to replica [{replica}] of [{repo_info.repo_path}]", output, err)
                err_count += 1
                continue

            self.log.info(f"pushed to replica [{replica}] of [{repo_info.repo_path}]. Took [{str(datetime.now()-start_time)}]")

        return err_count


def load_manifest(filename):
    repos = {}

    try:
        with open(filename) as f:
            repos = json.load(f)
    except Exception as err:
        return None, err

    for repo, man in repos.items():
        if type(man) is not dict:
            return None, Exception(f"expected [{repo}] repo definition to be a map, got [{type(man).__name__}]")

        if "origin" not in man:
            return None, Exception(f"missing [origin] field from [{repo}] repo definition")

        origin_tau = type(man["origin"])
        if origin_tau is not str:
            return None, Exception(f"expected [origin] field of [{repo}] repo to be a string, got [{origin_tau.__name__}]")

        if "replicas" not in man:
            return None, Exception(f"missing [replicas] field from [{repo}] repo definition")

        replicas_tau = type(man["replicas"])
        if replicas_tau is not dict:
            return None, Exception(f"expected [replicas] field of [{repo}] repo to be a map, got [{replicas_tau.__name__}]")

        for k, v in man["replicas"].items():
            if type(v) is not str:
                return None, Exception(f"expected replica [{k}] of [{repo}] repo to be a string, got [{type(v).__name__}]")

    return repos, None


def manf(man, f, *args):
    for repo_name, info in man.items():
        repo = RepoInfo()

        if info.get("skip"):
            continue

        repo.repo_name = repo_name
        repo.origin = info["origin"]
        repo.replicas = info["replicas"]
        repo.aliases = info.get("aliases", [])

        if not f(repo, *args):
            break


def do_mirror(repo_info, app, logger, errors):
    old_name = repo_info.repo_name
    app.set_alias_info(args.repo_dir, repo_info)

    if not repo_info.exists:
        logger.debug(f"repo [{repo_info.repo_name}] does not exist and no aliases found. Trying to clone it")

        if not app.clone_mirror(repo_info):
            errors += 1
            return True
    else:
        if repo_info.is_alias:
            logger.info(f"aliasing [{old_name}] as [{repo_info.repo_name}]")

        logger.debug(f"repo [{repo_info.repo_name}] is already cloned at [{repo_info.repo_path}]")

    for name, url in repo_info.replicas.items():
        if app.ls_remote(url) is None:
            logger.info(f"replica [{name}] of [{repo_info.repo_name}] doesn't exist at [{url}]")
            err = app.create_remote(url)
            if err is None:
                logger.info(f"created repo for replica [{name}] of [{repo_info.repo_name}] at [{url}]")
            else:
                logger.info(f"couldn't create repo for replica [{name}] of [{repo_info.repo_name}] at [{url}] due to [{err}]")
                continue

        if not app.add_replica(repo_info, name, url):
            errors += 1

    errors += app.sync(repo_info)

    return True


def do_integrity(repo_info, app, logger, errors):
    origin_hash = app.ls_remote(repo_info.origin)

    for name, url in repo_info.replicas.items():
        replica_hash = app.ls_remote(url)
        if replica_hash != origin_hash:
            errors += 1
            msg = f"head of repo [{repo_info.repo_name}] is at [{origin_hash}]"
            msg += f" but its replica [{name}] is at [{replica_hash}]"
            logger.error(msg)


if __name__ == "__main__":
    root = argparse.ArgumentParser(description='Automate repo mirroring')

    sub_parsers = root.add_subparsers()

    parent_parser = argparse.ArgumentParser(add_help=False)

    parent_parser.add_argument('-u', '--update-manifest', action="store_true")
    parent_parser.add_argument('-m', '--manifest', default="repos.json")
    parent_parser.add_argument('-v', '--log-level', default="info")
    parent_parser.add_argument('-l', '--log-file', default=f".logs/{APP_NAME}.log")
    parent_parser.add_argument('--dry-run', action="store_true")

    mirror_parser = sub_parsers.add_parser("mirror", parents=[parent_parser])
    mirror_parser.add_argument('-d', '--repo-dir', default=".repos")
    mirror_parser.set_defaults(func=do_mirror)

    integrity_parser = sub_parsers.add_parser("integrity", parents=[parent_parser])
    integrity_parser.set_defaults(func=do_integrity)

    args = root.parse_args()

    logger = get_logger(args.log_file)

    log_level = args.log_level.upper()
    if log_level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        log_level = "INFO"
        print(f"log level value [{args.log_level}] is invalid. Setting it to [{log_level}]")

    logger.setLevel(log_level)

    errors = 0

    try:
        app = App(logger, args.dry_run)

        repos, err = load_manifest(args.manifest)
        if err is not None:
            logger.fatal(f"cannot load manifest file due to err=[{err}]")
            sys.exit(1)

        if args.update_manifest:
            # If the manifest is source-controlled, pull the latest
            manifest_repo = os.path.dirname(args.manifest) or '.'
            _, err = app.run_command("test", "-d", ".git", cwd=manifest_repo)
            if err is None:
                logger.info("manifest is located in a git repository. Will try to update it")
                output, err = app.run_command("git", "pull", cwd=manifest_repo)
                if err is not None:
                    app.log_cmd_err("couldn't pull the manifest repo", output, err)
                else:
                    logger.info("updated manifest repo")

        if getattr(args, 'repo_dir', None):
            make_parents(args.repo_dir, True)

        errors = 0
        manf(repos, args.func, app, logger, errors)

        count = 'no' if errors == 0 else str(errors)
        plural = '' if errors == 1 else 's'
        logger.info(msg=f"Finished with {count} error{plural}")

        sys.exit(errors)
    except Exception as err:
        logger.error(err)
        sys.exit(1)
