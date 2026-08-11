import unittest

from core.character_session import CharacterSessionRegistry


class ActiveSetOwner:
    def __init__(self):
        self.active = set()

    def is_active(self, scope_key):
        return scope_key in self.active

    def activate(self, scope_key):
        if scope_key in self.active:
            return False
        self.active.add(scope_key)
        return True

    def deactivate(self, scope_key):
        self.active.discard(scope_key)

    def clear(self):
        self.active.clear()


class SessionActiveOwner:
    def __init__(self):
        self.registry = CharacterSessionRegistry()

    def session(self, scope_key):
        scope_type, scope_id = scope_key.split(':', 1)
        return self.registry.get_or_create(scope_type, scope_id)

    def is_active(self, scope_key):
        session = self.registry.get(scope_key)
        return False if session is None else session.is_active()

    def activate(self, scope_key):
        return self.session(scope_key).activate()

    def deactivate(self, scope_key):
        session = self.registry.get(scope_key)
        if session is not None:
            session.deactivate()

    def clear(self):
        for snapshot in self.registry.snapshots():
            session = self.registry.get(snapshot.scope_key)
            if session is not None:
                session.clear_runtime_state()


class ActiveScopeOwnerEquivalenceTests(unittest.TestCase):
    def owners(self):
        return ActiveSetOwner(), SessionActiveOwner()

    def test_single_scope_activate_deactivate_equivalence(self):
        old, candidate = self.owners()
        self.assertEqual(old.activate('group:7'), candidate.activate('group:7'))
        self.assertEqual(old.activate('group:7'), candidate.activate('group:7'))
        self.assertEqual(old.is_active('group:7'), candidate.is_active('group:7'))
        old.deactivate('group:7')
        candidate.deactivate('group:7')
        self.assertEqual(old.is_active('group:7'), candidate.is_active('group:7'))

    def test_message_and_task_reservation_share_one_active_bit(self):
        old, candidate = self.owners()
        self.assertEqual(old.activate('group:7'), candidate.activate('group:7'))
        self.assertEqual(old.activate('group:7'), candidate.activate('group:7'))
        old.deactivate('group:7')
        candidate.deactivate('group:7')
        self.assertEqual(old.activate('group:7'), candidate.activate('group:7'))

    def test_different_scopes_are_independent(self):
        old, candidate = self.owners()
        for scope_key in ('group:7', 'private:9'):
            self.assertEqual(old.activate(scope_key), candidate.activate(scope_key))
        self.assertEqual(old.is_active('group:7'), candidate.is_active('group:7'))
        self.assertEqual(old.is_active('private:9'), candidate.is_active('private:9'))
        old.deactivate('group:7')
        candidate.deactivate('group:7')
        self.assertEqual(old.is_active('private:9'), candidate.is_active('private:9'))

    def test_clear_cancellation_and_reactivation_equivalence(self):
        old, candidate = self.owners()
        old.activate('group:7')
        candidate.activate('group:7')
        old.activate('private:9')
        candidate.activate('private:9')
        old.clear()
        candidate.clear()
        for scope_key in ('group:7', 'private:9'):
            self.assertEqual(old.is_active(scope_key), candidate.is_active(scope_key))
            self.assertEqual(old.activate(scope_key), candidate.activate(scope_key))

    def test_deactivate_missing_scope_is_noop(self):
        old, candidate = self.owners()
        old.deactivate('group:missing')
        candidate.deactivate('group:missing')
        self.assertEqual(old.is_active('group:missing'), candidate.is_active('group:missing'))


if __name__ == '__main__':
    unittest.main()
