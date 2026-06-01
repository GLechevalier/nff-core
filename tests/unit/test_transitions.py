import pytest

from nff_ota.transitions import validate_transition


class TestDeviceTransitions:
    @pytest.mark.parametrize("from_,to", [
        ("pending", "accepted"),
        ("pending", "rejected"),
        ("preauthorized", "accepted"),
        ("accepted", "rejected"),
        ("accepted", "decommissioned"),
        ("rejected", "accepted"),
    ])
    def test_legal_transitions(self, from_: str, to: str):
        validate_transition("device", from_, to)  # must not raise

    @pytest.mark.parametrize("from_,to", [
        ("decommissioned", "accepted"),
        ("decommissioned", "pending"),
        ("accepted", "pending"),
        ("rejected", "pending"),
        ("pending", "decommissioned"),
        ("pending", "preauthorized"),
    ])
    def test_illegal_transitions_raise(self, from_: str, to: str):
        with pytest.raises(ValueError, match="Illegal device transition"):
            validate_transition("device", from_, to)

    def test_terminal_decommissioned_blocks_all(self):
        for target in ["pending", "accepted", "rejected", "preauthorized"]:
            with pytest.raises(ValueError):
                validate_transition("device", "decommissioned", target)


class TestDeploymentTransitions:
    @pytest.mark.parametrize("from_,to", [
        ("scheduled", "active"),
        ("scheduled", "aborted"),
        ("active", "paused"),
        ("active", "finished"),
        ("active", "aborted"),
        ("paused", "active"),
        ("paused", "aborted"),
    ])
    def test_legal_transitions(self, from_: str, to: str):
        validate_transition("deployment", from_, to)

    @pytest.mark.parametrize("from_,to", [
        ("finished", "active"),
        ("aborted", "active"),
        ("finished", "scheduled"),
        ("aborted", "paused"),
        ("paused", "finished"),
    ])
    def test_illegal_transitions_raise(self, from_: str, to: str):
        with pytest.raises(ValueError, match="Illegal deployment transition"):
            validate_transition("deployment", from_, to)


class TestDeviceDeploymentTransitions:
    @pytest.mark.parametrize("from_,to", [
        ("pending", "downloading"),
        ("pending", "aborted"),
        ("pending", "decommissioned"),
        ("downloading", "installing"),
        ("downloading", "failure"),
        ("downloading", "aborted"),
        ("installing", "rebooting"),
        ("installing", "failure"),
        ("rebooting", "success"),
        ("rebooting", "failure"),
        ("failure", "pending"),  # retry path
    ])
    def test_legal_transitions(self, from_: str, to: str):
        validate_transition("device_deployment", from_, to)

    @pytest.mark.parametrize("from_,to", [
        ("success", "pending"),
        ("success", "downloading"),
        ("aborted", "pending"),
        ("decommissioned", "pending"),
        ("already-installed", "pending"),
        ("rebooting", "downloading"),
        ("installing", "downloading"),
    ])
    def test_illegal_transitions_raise(self, from_: str, to: str):
        with pytest.raises(ValueError, match="Illegal device_deployment transition"):
            validate_transition("device_deployment", from_, to)


class TestUnknownEntity:
    def test_unknown_entity_raises(self):
        with pytest.raises(ValueError, match="Unknown entity type"):
            validate_transition("spaceship", "idle", "flying")


class TestErrorMessageContent:
    def test_message_includes_states(self):
        with pytest.raises(ValueError) as exc_info:
            validate_transition("device", "decommissioned", "accepted")
        msg = str(exc_info.value)
        assert "decommissioned" in msg
        assert "accepted" in msg
