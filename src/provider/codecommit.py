import os
import boto3
from provider import Provider


class CodeCommit(Provider):
    def __init__(self):
        self.client = boto3.client("codecommit")

    def match(self, url: str) -> bool:
        return "codecommit" in url

    def create_repo(self, url: str) -> str:
        name = os.path.splitext(os.path.basename(url))[0]
        metadata = self.client.create_repository(repositoryName=name)
        return metadata["repositoryMetadata"]["cloneUrlHttp"]

    def delete_repo(self, url: str) -> bool:
        name = os.path.splitext(os.path.basename(url))[0]
        metadata = self.client.delete_repository(repositoryName=name)
        return metadata["ResponseMetadata"]["RequestId"] != ""
