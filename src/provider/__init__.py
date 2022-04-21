from abc import ABC, abstractmethod


class Provider(ABC):
    @abstractmethod
    def match(self, url: str) -> bool():
        raise NotImplementedError()

    def create_repo(self, name: str) -> str:
        raise NotImplementedError()
