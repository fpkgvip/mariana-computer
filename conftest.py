import asyncio
import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Use a session-scoped loop for asyncio tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
