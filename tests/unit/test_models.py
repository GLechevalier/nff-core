import pytest
from pydantic import ValidationError

from nff_ota.models.artifact import UploadArtifactParams
from nff_ota.models.deployment import CreateDeploymentParams, ReportDeviceStatusParams
from nff_ota.models.device import RegisterDeviceParams
from nff_ota.models.group import CreateGroupParams, ModifyGroupMembersParams


class TestUploadArtifactParams:
    def test_valid(self):
        p = UploadArtifactParams(
            name="fw-esp32", version="1.0.0", device_types=["esp32"], file_path="/tmp/fw.bin"
        )
        assert p.name == "fw-esp32"
        assert p.device_types == ["esp32"]

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            UploadArtifactParams(name="", version="1.0.0", device_types=["esp32"], file_path="/tmp/fw.bin")

    def test_empty_version_rejected(self):
        with pytest.raises(ValidationError):
            UploadArtifactParams(name="fw", version="", device_types=["esp32"], file_path="/tmp/fw.bin")

    def test_empty_device_types_rejected(self):
        with pytest.raises(ValidationError):
            UploadArtifactParams(name="fw", version="1.0.0", device_types=[], file_path="/tmp/fw.bin")

    def test_default_provides_depends_empty(self):
        p = UploadArtifactParams(name="fw", version="1.0.0", device_types=["esp32"], file_path="/tmp/fw.bin")
        assert p.provides == {}
        assert p.depends == {}

    def test_multiple_device_types(self):
        p = UploadArtifactParams(
            name="fw", version="1.0.0", device_types=["esp32", "esp8266"], file_path="/tmp/fw.bin"
        )
        assert len(p.device_types) == 2


class TestRegisterDeviceParams:
    def test_valid(self):
        p = RegisterDeviceParams(identity_attributes={"mac": "aa:bb:cc:dd:ee:ff"})
        assert p.identity_attributes["mac"] == "aa:bb:cc:dd:ee:ff"

    def test_empty_identity_rejected(self):
        with pytest.raises(ValidationError):
            RegisterDeviceParams(identity_attributes={})

    def test_default_tags_empty(self):
        p = RegisterDeviceParams(identity_attributes={"mac": "aa:bb"})
        assert p.tags == []


class TestCreateDeploymentParams:
    def test_valid(self):
        p = CreateDeploymentParams(
            name="deploy-v1", artifact_id="art-123", device_group_id="grp-456"
        )
        assert p.max_devices_in_flight == 10
        assert p.retries == 3

    def test_max_devices_in_flight_minimum(self):
        with pytest.raises(ValidationError):
            CreateDeploymentParams(
                name="d", artifact_id="a", device_group_id="g", max_devices_in_flight=0
            )

    def test_retries_minimum(self):
        with pytest.raises(ValidationError):
            CreateDeploymentParams(
                name="d", artifact_id="a", device_group_id="g", retries=-1
            )

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            CreateDeploymentParams(name="", artifact_id="a", device_group_id="g")


class TestReportDeviceStatusParams:
    def test_valid_status(self):
        p = ReportDeviceStatusParams(
            device_id="dev-1", deployment_id="dep-1", status="downloading"
        )
        assert p.status == "downloading"

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            ReportDeviceStatusParams(device_id="d", deployment_id="d", status="unknown-status")


class TestCreateGroupParams:
    def test_valid(self):
        p = CreateGroupParams(name="production-fleet")
        assert p.name == "production-fleet"
        assert p.filter_criteria == {}

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            CreateGroupParams(name="")


class TestModifyGroupMembersParams:
    def test_valid(self):
        p = ModifyGroupMembersParams(group_id="grp-1", device_ids=["dev-1", "dev-2"])
        assert len(p.device_ids) == 2

    def test_empty_device_ids_rejected(self):
        with pytest.raises(ValidationError):
            ModifyGroupMembersParams(group_id="grp-1", device_ids=[])
