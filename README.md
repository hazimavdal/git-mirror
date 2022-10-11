# git-mirror

This is a script for one-way repo mirroring. It supports Github, Gitlab, and CodeCommit repositories.

## Pre-requisites

#### Setup SSH access for every backend used:

- Github: https://docs.github.com/en/github/authenticating-to-github/connecting-to-github-with-ssh
- Gitlab: https://docs.gitlab.com/ee/ssh/
- CodeCommit: https://docs.aws.amazon.com/codecommit/latest/userguide/setting-up-ssh-unixes.html

#### Install `pip` dependencies:

```bash
$ pip install -r requirements.txt
```

#### Setup API credentials (Optional)

If you expect the script to create mirror repositories that do not exist (that is, the origin exists but one or more of its replicas don't), then you must follow these steps for each host:

- Github: `git-mirror` currently does not support creating Github repos.

- Gitlab: 
    - Create a [personal token](https://docs.gitlab.com/ee/user/profile/personal_access_tokens.html) and save it in the environment variable `GITLAB_TOKEN`.
    - Optionally set `GITLAB_NAMESPACE` to a [namespace id](https://docs.gitlab.com/ee/api/namespaces.html) if you wish the new repository to be created in a particular namespace instead of your personal namespace.

- CodeCommit: Setup [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-configure.html) and its credentials.

## How it works

`git-mirror` takes as input a manifest file listing the repositories that need to be mirrored, structured as the following example:

```json
{
    "my-repo": {
        "description": "A script for one-way repo mirroring, supports Github, Gitlab, and CodeCommit",
        "skip": false,
        "origin": "git@github.com:hazimavdal/git-mirror.git",
        "replicas": {
            "gitlab": "git@gitlab.com:replica-set-01/my-repo-replica01.git",
            "aws": "ssh://git-codecommit.us-east-2.amazonaws.com/v1/repos/my-repo-replica02"
        }
    },
    ...
    "another repo": {
        ...
    }
    ...
}
```

In this example we have the repository `my-repo` whose origin is Github and it needs to be mirror to both Gitlab and CodeCommit. When the script is run, it will do the following:

1. Clone each repository in the manifest from the URL specified by `origin` (if it hasn't been cloned already). It is assumed the user running the script can `git clone` that repo.

2. `git push --mirror` to each replica in the `replicas` field. If the target repo does not exist, the script will try creating it.

## Usage

The script has two sub-commands with the following global arguments:

- `-m`, `--manifest`: the manifest file (defaults to `./repos.json`)
- `-v`, `--log-level`: log verbosity level (defaults to `info`)
- `-l`, `--log-file`: full path of the log file name (defaults to `.logs/git-mirror.log`)
- `--dry-run`: if set the script runs in dry-run mode without causing any side effects (defaults to `false`).

1. `mirror`: this is the main operation that performs one-way mirroring for all repositories in a manifest file. The command takes an additional argument:

- `-d`, `--repo-dir`: the directory name where repos should be cloned into (defaults to `.repos`)

Example usage:

```bash
./src/git-mirror.py  mirror                                    \
                    --manifest repos.sample.json               \
                    --log-file .logs/git-mirror.log            \
                    --repo-dir .repos                          \
                    --log-level debug
```

2. `integrity`: this command checks if the state of remotes matches what is specified by the manifest file. For every repo in the manifest, `git-mirror` checks if the `HEAD` of every replica matches the `HEAD` of the repository's `origin`. 

```bash
./src/git-mirror.py  integrity                                 \
                    --manifest repos.sample.json               \
                    --log-file .logs/git-mirror.log            \
                    --log-level debug
```


## Recommended usage

I run this script as a cron job on a VM that keeps my repos of interest in sync across hosts. My `repos.json` is kept in a separate repository and is pulled every time the cron job runs. This way, I can just modify this file and have the changes take effect seamlessly.

## Known issues

- The CodeCommit API ignores everything after a period (`.`) in the name of the repository that is being created. For example, if you attempt to create the repo `hello.world`, it will be created as `hello`, not `hello.world`. The script will fail in this case. However, if the repository already exists (i.e. not created through the script), then it works without issue.