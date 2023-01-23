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

    def delete_repo(self, url: str) -> bool:
        guid = os.path.splitext(os.path.basename(url))[0]

        projects = self.client.projects.list(owned=True)
        for proj in projects:
            if proj.name == guid:
                proj.delete()
                return True

        return False