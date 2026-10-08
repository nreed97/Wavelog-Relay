import asyncio

from wavelog_relay.wavelog import QsoUploader, WavelogError


class FlakyClient:
    def __init__(self, failures):
        self.failures = list(failures)
        self.sent = []

    async def post_adif(self, adif, spid):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((adif, spid))
        return {"imported": 1, "skipped": 0}


def test_retry_spool_and_reject(tmp_path):
    async def scenario():
        spool, failed = tmp_path / "spool.jsonl", tmp_path / "failed.adi"
        client = FlakyClient([WavelogError(0, "network_error", "down"),
                              WavelogError(400, "validation_error", "bad")])
        up = QsoUploader(client, spool, failed)
        up.submit("<CALL:2>A1 <EOR>", 1, "t", "A1")
        up.submit("<CALL:2>B2 <EOR>", 1, "t", "B2")
        assert len(spool.read_text().splitlines()) == 2
        # A restarted relay picks up the backlog from the spool file.
        assert QsoUploader(client, spool).backlog == 2

        task = asyncio.create_task(up.run())
        await asyncio.sleep(0.1)
        up._wake.set()            # skip the 5 s backoff
        for _ in range(50):
            if up.backlog == 0:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        return client, spool, failed, up

    client, spool, failed, up = asyncio.run(scenario())
    # A1 failed transiently then hit a validation error -> written to failed file; B2 uploaded.
    assert up.backlog == 0 and spool.read_text() == ""
    assert "A1" in failed.read_text()
    assert [s[0] for s in client.sent] == ["<CALL:2>B2 <EOR>"]


def test_dedupe():
    up = QsoUploader(FlakyClient([]), None)
    assert up.submit("x", 1, "s", "A", dedupe_key="k")
    assert not up.submit("x", 1, "s", "A", dedupe_key="k")
