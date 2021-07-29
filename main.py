#!/usr/bin/python3
import os
import sys
import json
import logging
import argparse
import subprocess as proc
from logging import handlers

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


def run_command(cmd, *args, cwd=None):
    if cwd is not None and len(cwd.strip()) == 0:
        cwd = "."

    try:
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


class App:
    def __init__(self, logger):
        self.log = logger

    def log_cmd_err(self, msg, output, err):
        self.log.error(f"{msg} due to err=[{err}]. stdout=[{output['stdout']}], stderr=[{output['stderr']}]")

    def create_mirror(self, mirrors_dir, repo, origin):
        output, err = run_command("git", "clone", "--mirror", origin, repo, cwd=mirrors_dir)

        if err is not None:
            self.log_cmd_err(f"cannot create mirror for '{repo}'", output, err)
            return False

        self.log.info(f"created mirror repo '{repo}' with origin='{origin}'")

        return True

    def add_target(self, repo_path, target_name, target_url):
        output, err = run_command("git", "config", "--get", f"remote.{target_name}.url", cwd=repo_path)

        # Check if this target with the same URL already exists
        if err is None and output["stdout"].split('\n')[0] == target_url:
            self.log.debug(f"target '{target_name}' already exists in '{repo_path}'")
            return True

        output, err = run_command("git", "remote", "add",
                                  "--mirror=push", target_name,
                                  target_url, cwd=repo_path)

        if err is not None:
            self.log_cmd_err(f"cannot add target '{target_name}' to '{repo}'", output, err)
            return False

        self.log.info(f"added target '{target_name}:{target_url}' to '{repo}'")

        return True

    def sync(self, repo_path, targets):
        output, err = run_command("git",  "fetch", "--prune", "origin", cwd=repo_path)

        if err is not None:
            self.log_cmd_err(f"cannot fetch '{repo_path}'", output, err)
            return False

        self.log.info(f"fetched '{repo_path}'")

        success = True
        for target in targets:
            output, err = run_command("git", "push", "--mirror", target, cwd=repo_path)

            if err is not None:
                self.log_cmd_err(f"cannot push target '{target}' of '{repo}'", output, err)
                success = False
                continue

            self.log.info(f"pushed target '{target}' of '{repo_path}'")

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

    args = parser.parse_args()

    logger = get_logger(args.log_file)

    log_level = args.log_level.upper()
    if log_level not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        log_level = "INFO"
        print(f"log level value '{args.log_level}' is invalid. Setting it to INFO")

    logger.setLevel(log_level)

    app = App(logger)
    errors = 0

    try:
        repos, err = load_manifest(args.manifest)
        if err is not None:
            logger.fatal(f"cannot load manifest file due to err=[{err}]")
            sys.exit(1)

        # If the manifest is source-controlled, pull the latest
        manifest_repo = os.path.dirname(args.manifest) or '.'
        _, err = run_command("ls", ".git", cwd=manifest_repo)
        if err is None:
            logger.info("manifest is located in a git repository. Will try to update it")
            output, err = run_command("git", "pull", cwd=manifest_repo)
            if err is not None:
                app.log_cmd_err("couldn't pull the manifest repo", output, err)
            else:
                logger.info("updated manifest repo")

        make_parents(args.repo_dir, True)

        for repo, man in repos.items():
            origin = man["origin"]
            replicas = man["replicas"]

            repo_path = os.path.join(args.repo_dir, repo)

            if man.get("skip"):
                logger.info(f"skipping repo '{repo}'")
                continue

            if not os.path.exists(repo_path):
                if not app.create_mirror(args.repo_dir, repo, origin):
                    errors += 1
                    continue

            for name, url in replicas.items():
                if not app.add_target(repo_path, name, url):
                    errors += 1

            if not app.sync(repo_path, replicas.keys()):
                errors += 1

        sys.exit(errors)
    except Exception as err:
        logger.error(err)
        sys.exit(1)
