# openddil-sensor-ingest/fixtures/README.md

## Test fixtures for binary DIS ingestion

Used by `dis_ingestor.py`, `sim-dis-mapping.yaml`, and the Hero Scenario v3
test suite (`openddil-demo/tests/hero_scenario_v3/`).

| File                       | Bytes | DIS PDU type   | Purpose                                                            |
|----------------------------|-------|----------------|--------------------------------------------------------------------|
| `sample_entity_state.bin`  | 144   | 1 (EntityState)| Valid M1A2 SEPv3 PDU — the canonical success-path fixture          |
| `sample_fire_pdu.bin`      |  96   | 2 (Fire)       | Non-EntityState — verifies "drop and count" behavior               |
| `sample_malformed.bin`     |  32   | n/a (noise)    | `os.urandom(32)` — verifies error counter + sidecar resilience     |

### sample_entity_state.bin

- EntityID: site=1, application=1, entity=4773
- EntityType: 1.1.225.1.3.1.0 (M1A2 SEPv3 per `openddil-contracts/ontology/dis_entity_types.yaml`)
- Force: friendly (1)
- Marking: `IRON-01`
- Provenance: serialized by `opendis==1.0` via `generate_fixtures.py`,
  verified round-trippable through `opendis.PduFactory.createPdu`
  (2026-05-12).

### sample_fire_pdu.bin

- pduType=2 (Fire), protocolFamily=2 (Warfare)
- Provenance: serialized by `opendis==1.0`, round-trip verified.

### sample_malformed.bin

- 32 bytes of `os.urandom`.
- Verified that `opendis.PduFactory.createPdu` returns None (the sidecar
  must increment `dis_decode_errors_total` and not crash).

## Regeneration

```powershell
# From this directory (requires opendis==1.0)
py generate_fixtures.py
```

The generator both writes the binary files and verifies each one parses
back through opendis. If the verification step prints anything other than
three `OK` lines, do NOT commit the resulting fixtures.

## History

- **2026-05-12** — Regenerated. The previous fixture set (hand-curated
  hex blob) was 160 bytes for the Entity State PDU and *could not* be
  parsed by opendis 1.0 (`struct.error: unpack requires a buffer of 1
  bytes`, raised mid-parse of the marking field). Today's fixtures are
  emitted by opendis's own `serialize()`, so they are guaranteed to
  match the parser's expectations.
