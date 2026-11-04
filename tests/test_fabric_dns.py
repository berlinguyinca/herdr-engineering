from __future__ import annotations

import pytest

from herdr_engineering import fabric_dns as dns


class FakeR53:
    def __init__(self, zones, records=None):
        self.zones, self.records, self.changes = zones, records or {}, []

    def get_paginator(self, _):
        zones = self.zones

        class P:
            def paginate(self):
                return [{"HostedZones": zones}]
        return P()

    def list_resource_record_sets(self, HostedZoneId, StartRecordName, **_):
        rr = self.records.get(StartRecordName.rstrip("."))
        return {"ResourceRecordSets": [rr] if rr else []}

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):
        self.changes.append((HostedZoneId, ChangeBatch))


ZONES = [{"Id": "/hostedzone/ZPARENT", "Name": "example.test.", "Config": {}},
         {"Id": "/hostedzone/ZSUB", "Name": "local.example.test.", "Config": {}},
         {"Id": "/hostedzone/ZPRIV", "Name": "local.example.test.", "Config": {"PrivateZone": True}}]


def test_only_tailnet_ipv4_addresses_publishable():
    assert dns.validate_ips(["100.104.39.6", "100.104.39.6"]) == ["100.104.39.6"]
    for bad in ("192.168.1.5", "8.8.8.8", "fd7a:115c:a1e0::8c2b:2707", "nope", "127.0.0.1"):
        with pytest.raises(dns.DnsError):
            dns.validate_ips([bad])
    with pytest.raises(dns.DnsError):
        dns.validate_ips([])


def test_change_batch_is_a_records_only_for_apex_and_wildcard():
    b = dns.change_batch("Local.Example.Test.", ["100.64.1.1", "100.64.2.2"], ttl=30)
    assert [c["ResourceRecordSet"]["Name"] for c in b["Changes"]] == ["local.example.test.", "*.local.example.test."]
    assert {c["ResourceRecordSet"]["Type"] for c in b["Changes"]} == {"A"}
    assert {c["Action"] for c in b["Changes"]} == {"UPSERT"}
    assert [r["Value"] for r in b["Changes"][0]["ResourceRecordSet"]["ResourceRecords"]] == ["100.64.1.1", "100.64.2.2"]


def test_zone_is_longest_public_match():
    assert dns.find_zone(FakeR53(ZONES), "x.local.example.test") == "ZSUB"
    assert dns.find_zone(FakeR53(ZONES[:1]), "local.example.test") == "ZPARENT"
    with pytest.raises(dns.DnsError):
        dns.find_zone(FakeR53(ZONES), "other.org")


def test_publish_dry_run_and_apply_and_idempotent():
    c = FakeR53(ZONES[:1])
    r = dns.publish(c, "local.example.test", ["100.64.1.1"], dry_run=True)
    assert r["changed"] and not r["applied"] and c.changes == []
    r = dns.publish(c, "local.example.test", ["100.64.1.1"])
    assert r["applied"] and len(c.changes) == 1 and c.changes[0][0] == "ZPARENT"
    same = {"Name": "local.example.test.", "Type": "A", "ResourceRecords": [{"Value": "100.64.1.1"}]}
    wild = {"Name": "\\052.local.example.test.", "Type": "A", "ResourceRecords": [{"Value": "100.64.1.1"}]}
    c2 = FakeR53(ZONES[:1], {"local.example.test": same, "*.local.example.test": wild})
    c2.records["*.local.example.test"]["Name"] = "\\052.local.example.test."
    r = dns.publish(c2, "local.example.test", ["100.64.1.1"])
    assert not r["changed"] and not r["applied"] and c2.changes == []


def test_bad_ip_rejected_before_any_api_call():
    c = FakeR53(ZONES)
    with pytest.raises(dns.DnsError):
        dns.publish(c, "local.example.test", ["10.0.0.1"])
    assert c.changes == []
