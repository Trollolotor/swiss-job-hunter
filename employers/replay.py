"""Offline fixture replay cannot make network requests."""


def replay_context(observations):
    class Replay:
        def __init__(self, policy, settings):
            self.settings = settings
            self.observations = []
            self.pending = list(observations)
            self.requests, self.bytes = 0, 0
            self.known = {}

        def allowed(self, *args):
            pass

        async def fetch(self, url, method="GET", body=None):
            match = next(
                (
                    i
                    for i, o in enumerate(self.pending)
                    if o.url == url and o.method == method and o.request_body == body
                ),
                None,
            )
            if match is None:
                raise ValueError("Fixture does not contain the requested observation")
            obs = self.pending.pop(match)
            self.observations.append(obs)
            self.requests += 1
            self.bytes += len(obs.text.encode())
            return obs

        async def close(self):
            pass

    return Replay
