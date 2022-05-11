import os
import gitlab
from provider import Provider


class Gitlab(Provider):
    def __init__(self, namespace: str, token: str):
        self.namespace = namespace

        self.client = gitlab.Gitlab("https://gitlab.com", private_token=token)

    def match(self, url: str) -> bool:
        return "gitlab" in url

    def create_repo(self, url: str) -> str:
        name = os.path.splitext(os.path.basename(url))[0]

        return self.client.projects.create({
            "name": name,
            "namespace_id": self.namespace
        }).ssh_url_to_repo
