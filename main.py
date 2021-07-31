#!/usr/bin/python3
import os
import sys
import json
import logging
import argparse
import subprocess as proc
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
        self.targets = []


class App:
    def __init__(self, logger, dry_run):
        self.log = logger
        self.dry_run = dry_run

        if self.dry_run:
            self.log.info("starting in dry-run mode")

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
            err = Exception(f"'{cmd}' command exited with status {exec.returncode}")

        return {'stdout': exec.stdout, 'stderr': exec.stderr}, err

    def log_cmd_err(self, msg, output, err):
        self.log.error(f"{msg} due to err=[{err}]. stdout=[{output['stdout']}], stderr=[{output['stderr']}]")

    def locate_repo(self, repo_dir, repo_name, aliases):
        info = RepoInfo()

        info.repo_dir = repo_dir
        info.repo_name = repo_name
        info.repo_path = os.path.join(repo_dir, repo_name)
        info.exists = os.path.exists(info.repo_path)
        info.is_alias = False

        if info.exists:
            return info

        for name in aliases:
            repo_path = os.path.join(repo_dir, name)

            if os.path.exists(repo_path):
                info.repo_name = name
                info.repo_path = repo_path
                info.is_alias = True
                info.exists = True

                return info

        return info

    def clone_mirror(self, repo_info):
        start_time = datetime.now()

        self.log.info(f"cloning mirror repo '{repo_info.repo_name}' with origin='{repo_info.origin}' into '{repo_info.repo_dir}'")

        output, err = self.run_command("git", "clone", "--mirror", repo_info.origin, repo_info.repo_name, cwd=repo_info.repo_dir)

        if err is not None:
            self.log_cmd_err(f"cannot clone mirror for '{repo_info.repo_name}'", output, err)
            return False

        duration = str(datetime.now() - start_time)
        self.log.info(f"cloned mirror repo '{repo_info.repo_name}' with origin='{repo_info.origin}' into '{repo_info.repo_dir}'. Took '{duration}'")

        return True

    def add_target(self, repo_info, target_name, target_url):
        output, err = self.run_command("git", "config", "--get", f"remote.{target_name}.url", cwd=repo_info.repo_path)

        # Check if this target with the same URL already exists
        if err is None and output["stdout"].split('\n')[0] == target_url:
            self.log.debug(f"target '{target_name}' already exists in '{repo_info.repo_path}'")
            return True

        output, err = self.run_command("git", "remote", "add",
                                       "--mirror=push", target_name,
                                       target_url, cwd=repo_info.repo_path)

        if err is not None:
            self.log_cmd_err(f"cannot add target '{target_name}' to '{repo_info.repo_path}'", output, err)
            return False

        self.log.info(f"added target '{target_name}:{target_url}' to '{repo_info.repo_path}'")

        repo_info.targets.append(target_name)

        return True

    def sync(self, repo_info):
        self.log.info(f"fetching '{repo_info.repo_path}' origin")

        start_time = datetime.now()
        output, err = self.run_command("git",  "fetch", "--prune", "origin", cwd=repo_info.repo_path)

        if err is not None:
            self.log_cmd_err(f"cannot fetch '{repo_info.repo_path}'", output, err)
            return False

        self.log.info(f"fetched '{repo_info.repo_path}'. Took '{str(datetime.now()-start_time)}'")

        success = True
        for target in repo_info.targets:
            self.log.info(f"pushing '{target}' target of '{repo_info.repo_path}'")

            start_time = datetime.now()
            output, err = self.run_command("git", "push", "--mirror", target, cwd=repo_info.repo_path)

            if err is not None:
                self.log_cmd_err(f"cannot push target '{target}' of '{repo_info.repo_path}'", output, err)
                success = False
                continue

            self.log.info(f"pushed target '{target}' of '{repo_info.repo_path}'. Took '{str(datetime.now()-start_time)}'")

        return success


def load_manifest(filename):
    repos = {}

    try:
        with open(filename) as f:
            repos = json.load(f)
    except Exception as err:
        return None, err

    for repo, man in repos.items():
        if type(man) is not dict:
            return None, Exception(f"expected '{repo}' repo definition to be a map, got {type(man).__name__}")

        if "origin" not in man:
            return None, Exception(f"missing 'origin' field from '{repo}' repo definition")

        origin_tau = type(man["origin"])
        if origin_tau is not str:
            return None, Exception(f"expected 'origin' field of '{repo}' repo to be a string, got {origin_tau.__name__}")

        if "replicas" not in man:
            return None, Exception(f"missing 'replicas' field from '{repo}' repo definition")

        replicas_tau = type(man["replicas"])
        if replicas_tau is not dict:
            return None, Exception(f"expected 'replicas' field of '{repo}' repo to be a map, got {replicas_tau.__name__}")

        for k, v in man["replicas"].items():
            if type(v) is not str:
                return None, Exception(f"expected replica '{k}' of '{repo}' repo to be a string, got {type(v).__name__}")

    return repos, None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Automate repo mirroring')

    parser.add_argument('-m', '--manifest', default="repos.json")
    parser.add_argument('-d', '--repo-dir', default=".repos")
    parser.add_argument('-v', '--log-level', default="info")
    parser.add_argument('-l', '--log-file', default=f".logs/{APP_NAME}.log")
    parser.add_argument('--dry-run', action="store_true")

    args = parser.parse_args()

    logger = get_logger(args.log_file)

    log_level = args.log_level.upper()
    if log_level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        log_level = "INFO"
        print(f"log level value '{args.log_level}' is invalid. Setting it to INFO")

    logger.setLevel(log_level)

    app = App(logger, args.dry_run)
    errors = 0

    try:
        repos, err = load_manifest(args.manifest)
        if err is not None:
            logger.fatal(f"cannot load manifest file due to err=[{err}]")
            sys.exit(1)

        # If the manifest is source-controlled, pull the latest
        manifest_repo = os.path.dirname(args.manifest) or '.'
        _, err = app.run_command("ls", ".git", cwd=manifest_repo)
        if err is None:
            logger.info("manifest is located in a git repository. Will try to update it")
            output, err = app.run_command("git", "pull", cwd=manifest_repo)
            if err is not None:
                app.log_cmd_err("couldn't pull the manifest repo", output, err)
            else:
                logger.info("updated manifest repo")

        make_parents(args.repo_dir, True)

        for repo_main_name, man in repos.items():
            origin = man["origin"]
            replicas = man["replicas"]
            aliases = man.get("aliases", [])

            if man.get("skip"):
                logger.info(f"skipping repo '{repo_main_name}'")
                continue

            repo_info = app.locate_repo(args.repo_dir, repo_main_name, aliases)
            repo_info.origin = origin

            if not repo_info.exists:
                logger.debug(f"repo '{repo_info.repo_name}' does not exist and no aliases found. Trying to clone it")

                if not app.clone_mirror(repo_info):
                    errors += 1
                    continue
            else:
                if repo_info.is_alias:
                    logger.info(f"aliasing '{repo_main_name}' as '{repo_info.repo_name}'")

                logger.debug(f"repo '{repo_info.repo_name}' is already cloned at '{repo_info.repo_path}'")

            for name, url in replicas.items():
                if not app.add_target(repo_info, name, url):
                    errors += 1

            if not app.sync(repo_info):
                errors += 1

        sys.exit(errors)
    except Exception as err:
        logger.error(err)
        sys.exit(1)
