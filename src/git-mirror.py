#!/usr/bin/python3
import os
import re
import sys
import json
import time
import logging
import argparse
import subprocess as proc
from decouple import config
from logging import handlers
from datetime import datetime

from provider.gitlab import Gitlab
from provider.codecommit import CodeCommit
import cerberus as cer


repo_schema = {
    "guid": {
        "type": "string",
        "required": True,
        "regex": r"[a-z][-_\w]*"
    },
    "origin": {
        "type": "string",
        "required": True,
    },
    "description": {
        "type": "string",
        "required": False,
    },
    "skip": {
        "type": "boolean",
        "default": False,
    },
    "replicas": {
        "type": "dict",
        "keysrules": {
                "type": "string",
            "regex": "[a-z]+"
        },
        "valuesrules": {
            "type": "string",
            "required": True
        },
    },
}


APP_NAME = "git-mirror"


def make_parents(filename, dir=False):
    base = filename if dir else os.path.dirname(filename)
    if base and not os.path.exists(base):
        os.makedirs(base)


def get_logger(filename):
    base, name_ext = os.path.split(filename)
    name, ext = os.path.splitext(name_ext)
    sign = time.strftime('%Y-%m-%d')
    filename = os.path.join(base, f"{name}_{sign}{ext}")

    class WrappedLogger(logging.Logger):
        def __init__(self, name, level=logging.NOTSET):
            self._error_count = 0
            super(WrappedLogger, self).__init__(name, level)

        @property
        def error_count(self):
            return self._error_count

        def error(self, msg, *args, **kwargs):
            self._error_count += 1
            return super(WrappedLogger, self).error(msg, *args, **kwargs)

    logging.setLoggerClass(WrappedLogger)

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


class RepoInfo:
    def __init__(self):
        self.repo_dir = None
        self.repo_name = None
        self.repo_path = None
        self.exists = False

        self.origin = None
        self.replicas = {}


class App:
    def __init__(self, logger, dry_run):
        self.log = logger
        self.dry_run = dry_run

        if self.dry_run:
            self.log.info("starting in dry-run mode")

        self.providers = []
        if config("GIT_MIRROR_USE_GITLAB", default=False):
            provider = Gitlab(config("GITLAB_NAMESPACE"),
                              config("GITLAB_TOKEN"))

            self.providers.append(provider)
            self.log.info(f"using gitlab provider")

        if config("GIT_MIRROR_USE_CODECOMMIT", default=False):
            self.providers.append(CodeCommit())
            self.log.info(f"using codecommit provider")

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

    def repo_exists(self, repo):
        head = app.ls_remote(repo)
        return head is not None and head.get("HEAD") != ""

    def ls_remote(self, repo):
        output, err = app.run_command("git", "ls-remote", repo)

        if err != None:
            self.log.debug(f"ls-remote failed with err=[{err}], output=[{output}]")
            return None

        return {l[1]: l[0] for l in [line.split('\t') for line in output["stdout"].split("\n")] if len(l) == 2}

    def delete_remote(self, url):
        for provider in self.providers:
            if provider.match(url):
                return provider.delete_repo(url)

        raise Exception(f"no provider found for url=[{url}]")

    def create_remote(self, url):
        try:
            for provider in self.providers:
                if provider.match(url):
                    return None, provider.create_repo(url)

            raise Exception(f"no provider found for url=[{url}]")
        except Exception as err:
            return err, ""

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
    repos = []

    try:
        with open(filename) as f:
            repos = json.load(f)
    except Exception as err:
        return None, err

    if type(repos) is not list:
        return None, Exception(f"expected a list of repo definitions, got {type(repos).__name__}")
    for repo in repos:
        validator = cer.Validator()
        if not validator.validate(repo, repo_schema):
            return None, Exception(f"manifest failed schema validation due to [{validator.errors}]")

    return repos, None


def manf(repos, f, *args):
    for repo in repos:
        if repo.get("skip"):
            continue

        info = RepoInfo()
        info.repo_name = repo.get("guid")
        info.origin = repo.get("origin")
        info.replicas = repo.get("replicas")

        if not f(info, *args):
            break


def do_mirror(repo_info, app: App, logger, args):
    repo_info.repo_dir = args.repo_dir
    repo_info.repo_path = os.path.join(repo_info.repo_dir, repo_info.repo_name)
    repo_info.exists = os.path.exists(repo_info.repo_path)

    if not repo_info.exists:
        logger.debug(f"repo [{repo_info.repo_name}] does not exist. Trying to clone it")

        if not app.clone_mirror(repo_info):
            return True
    else:
        logger.debug(f"repo [{repo_info.repo_name}] is already cloned at [{repo_info.repo_path}]")

    for name, url in repo_info.replicas.items():
        if not app.repo_exists(url):
            logger.info(f"replica [{name}] of [{repo_info.repo_name}] doesn't exist at [{url}]")
            err, remote_url = app.create_remote(url)
            if err is None:
                logger.info(f"created repo for replica [{name}] of [{repo_info.repo_name}] at [{remote_url}]")
            else:
                logger.info(f"couldn't create repo for replica [{name}] of [{repo_info.repo_name}] at [{url}] due to [{err}]")
                continue

        app.add_replica(repo_info, name, url)

    app.sync(repo_info)

    return True


def do_integrity(repo_info, app: App, logger, _):
    origin_branches = app.ls_remote(repo_info.origin)
    if "HEAD" in origin_branches:
        del origin_branches["HEAD"]

    if origin_branches is None:
        logger.error(f"repo [{repo_info.repo_name}:{repo_info.origin}] does not exist")
        return True

    for name, url in repo_info.replicas.items():
        replica_branches = app.ls_remote(url)
        if "HEAD" in replica_branches:
            del replica_branches["HEAD"]

        if replica_branches is None:
            logger.error(f"cannot ls-remote replica [{name}] of [{repo_info.repo_name}]")
            return True

        diff = set(origin_branches.items()) ^ set(replica_branches.items())
        if len(diff) > 0:
            logger.error(f"replica [{name}] differs from the origin of [{repo_info.repo_name}]: diff=[{diff}]")
        else:
            logger.info(f"replica [{name}] of [{repo_info.repo_name}] is in sync")

    return True


def do_purge(repo_info: RepoInfo, app: App, logger, args):
    for target, url in repo_info.replicas.items():
        if target == args.target:
            logger.info(f"Purging {url}")
            if app.delete_remote(url):
                logger.info(f"Deleted {url}")

    return True


if __name__ == "__main__":
    root = argparse.ArgumentParser(description='Automate repo mirroring')

    sub_parsers = root.add_subparsers()

    parent_parser = argparse.ArgumentParser(add_help=False)

    parent_parser.add_argument('-m', '--manifest', default="repos.json")
    parent_parser.add_argument('-v', '--log-level', default="info")
    parent_parser.add_argument('-l', '--log-file', default=f".logs/{APP_NAME}.log")
    parent_parser.add_argument('--dry-run', action="store_true")

    mirror_parser = sub_parsers.add_parser("mirror", parents=[parent_parser])
    mirror_parser.add_argument('-d', '--repo-dir', default=".repos")
    mirror_parser.set_defaults(func=do_mirror)

    integrity_parser = sub_parsers.add_parser("integrity", parents=[parent_parser])
    integrity_parser.set_defaults(func=do_integrity)

    purge_parser = sub_parsers.add_parser("purge", parents=[parent_parser])
    purge_parser.add_argument('-t', '--target', required=True, choices=["gitlab", "aws"])
    purge_parser.set_defaults(func=do_purge)

    args = root.parse_args()

    if not getattr(args, "func", None):
        root.print_usage()
        sys.exit(1)

    logger = get_logger(args.log_file)

    log_level = args.log_level.upper()
    if log_level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        log_level = "INFO"
        print(f"log level value [{args.log_level}] is invalid. Setting it to [{log_level}]")

    logger.setLevel(log_level)

    try:
        app = App(logger, args.dry_run)

        repos, err = load_manifest(args.manifest)
        if err is not None:
            logger.fatal(f"cannot load manifest file due to err=[{err}]")
            sys.exit(1)

        if getattr(args, 'repo_dir', None):
            make_parents(args.repo_dir, True)

        manf(repos, args.func, app, logger, args)

        count = 'no' if logger.error_count == 0 else str(logger.error_count)
        plural = '' if logger.error_count == 1 else 's'
        logger.info(f"Finished with {count} error{plural}")

        sys.exit(logger.error_count)
    except Exception as err:
        logger.error(err)
        sys.exit(1)
