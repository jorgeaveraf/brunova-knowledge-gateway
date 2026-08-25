from datetime import timedelta

import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.hubspot.models import PendingAuthorization, utc_now
from app.adapters.hubspot.token_store import HubSpotTokenStore, ObjectConflict


class MemoryBackend:
    def __init__(self):
        self.objects = {}
        self.generation = 0

    def read(self, object_name):
        item = self.objects.get(object_name)
        return (None, 0) if item is None else item

    def write(self, object_name, content, *, generation):
        current = self.objects.get(object_name)
        current_generation = 0 if current is None else current[1]
        if current_generation != generation:
            raise ObjectConflict
        self.generation += 1
        self.objects[object_name] = (content, self.generation)
        return self.generation

    def delete(self, object_name, *, generation):
        current = self.objects.get(object_name)
        if current is None or current[1] != generation:
            raise ObjectConflict
        del self.objects[object_name]


def pending(state, *, expired=False):
    now = utc_now()
    import hashlib

    return PendingAuthorization(
        state_digest=hashlib.sha256(state.encode()).hexdigest(),
        code_verifier="v" * 64,
        created_at=now - timedelta(minutes=20) if expired else now,
        expires_at=now - timedelta(seconds=1) if expired else now + timedelta(minutes=10),
    )


def store():
    backend = MemoryBackend()
    return HubSpotTokenStore(
        backend, prefix="oauth/hubspot", encryption_secret="ephemeral-test-secret"
    ), backend


def test_pending_state_is_single_use_and_not_stored_raw():
    token_store, backend = store()
    state = "state-value-that-must-not-be-persisted-raw"
    token_store.create_pending(state, pending(state))

    assert state.encode() not in next(iter(backend.objects.values()))[0]
    assert token_store.consume_pending(state).code_verifier == "v" * 64
    with pytest.raises(WorkspaceAdapterError, match="already used"):
        token_store.consume_pending(state)


def test_expired_pending_state_is_deleted_and_rejected():
    token_store, backend = store()
    state = "expired-state-value-that-is-long-enough"
    token_store.create_pending(state, pending(state, expired=True))

    with pytest.raises(WorkspaceAdapterError) as raised:
        token_store.consume_pending(state)

    assert raised.value.code == "hubspot_oauth_state_expired"
    assert not backend.objects


def test_refresh_token_is_encrypted_and_rotation_is_atomic():
    token_store, backend = store()
    token_store.save_connection("refresh-token-one", scope="crm.objects.contacts.read")
    connection_content = backend.objects["oauth/hubspot/connection.json"][0]
    assert b"refresh-token-one" not in connection_content

    lease = token_store.acquire_refresh_lease()
    assert lease.refresh_token == "refresh-token-one"
    with pytest.raises(WorkspaceAdapterError) as raised:
        token_store.acquire_refresh_lease()
    assert raised.value.code == "hubspot_refresh_in_progress"

    token_store.complete_refresh(lease, "refresh-token-two", scope=None)
    rotated = token_store.acquire_refresh_lease()
    assert rotated.refresh_token == "refresh-token-two"
    assert b"refresh-token-two" not in backend.objects["oauth/hubspot/connection.json"][0]


def test_invalid_refresh_marks_connection_for_reauthorization():
    token_store, _backend = store()
    token_store.save_connection("refresh-token", scope=None)
    lease = token_store.acquire_refresh_lease()
    token_store.fail_refresh(lease, invalid=True)

    status = token_store.status()
    assert status.connected is False
    assert status.status == "reauthorization_required"
