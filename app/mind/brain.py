"""Reasoning facade. Only Voice owns provider transport and credentials."""


class Brain:
    def __init__(self, gateway):
        self._gateway = gateway

    def generate_content(self, prompt, **kwargs):
        return self._gateway.generate_content(prompt, **kwargs)

    def test_all_connections(self):
        return self._gateway.test_all_connections()
