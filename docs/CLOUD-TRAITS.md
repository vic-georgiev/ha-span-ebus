# SPAN Cloud — Trait Snapshot and Configuration

What has been decoded about the configuration SPAN's cloud serves to its Home app (the trait snapshot), and about its energy history, how each
finding was confirmed, and where it is used. Companion to `CLOUD-PROTO.md` (telemetry and commands) and `CLOUD-FLOW.md` (sign-in and transport).
Read this before changing a decoder, and add to it when something new is pinned down: each finding says what the evidence was, so the next person
can tell a confirmed fact from a guess.

Where the findings are used: `span_client/cloud_traits.py` here decodes names, spaces, ratings and relays. The cloud transport of the official
integration in [vic-georgiev/span](https://github.com/vic-georgiev/span) (branch `cloud-transport`, `custom_components/span_panel/cloud/`) vendors
this client and adds the load-management and limit decoders described below (`parse_panel_traits`, `_load_management_for`).

Status words used below:

- **Confirmed**: checked value by value against the SPAN app on two panels.
- **Strong**: one consistent explanation fits every value seen, but it has not been checked against the app.
- **Guess**: a candidate only.

## Sources of Data

| Source                  | Transport                                                       | What it carries                                                                  |
| ----------------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Sign-in                 | Cognito (email and password, tokens kept on the user's machine) | access, id and refresh tokens                                                    |
| `SubscribeAndGetTraits` | gRPC, `io.span.services.mobilefrontend.MobileFrontendService`   | the trait snapshot: configuration, names, relay state. Re-read every few minutes |
| Telemetry               | Ably channel named by the subscribe call                        | power, current, voltage frames, about once a second                              |
| `GetHistoryAggregation` | gRPC, same service                                              | SPAN's own metered kWh in buckets (minute, quarter-hour, hour, day, month)       |
| `ListLatestTraitData`   | gRPC, same service                                              | raw latest trait values (not yet used)                                           |
| `ListDispatches`        | gRPC, same service                                              | probably the alerts feed; refused with status 3 (request shape still wrong)      |
| Energy export e-mail    | CSV, requested in the app                                       | hourly kWh per circuit and per flow, one panel per file (see below)              |

Each SPAN site is one account "home". Two panels can be separate sites even in one house: a site holds the panel's gateway and, if installed, the
battery. Telemetry instance ids are per panel, not per site.

## Reading the Trait Snapshot

The snapshot is a list of resources. Each resource has a hardware id (field 1.1) and a list of entries (field 2). Each entry is:

```text
entry {
  1: key       { 1: vendor_id, 2: product_id, 3: trait_id, 4: version }
  2: instance  { 1: instance_id }
  3: value     { 1: metadata, 2: bytes }   # the bytes parse as { 1: <trait message> }
}
```

Traits are named `vendor/trait`, so `1/31` is SPAN's trait 31. A **TraitRef** is a small message `{1: key, 2: instance}` pointing at another
trait instance; circuits are stitched together by following them.

The inventory tool (`tools/trait_inventory.py`) dumps a snapshot read-only, redacting serials, codes and coordinates, and prints every leaf as a
dotted path. In its reports, `1.2.2.1=4` means: field 1 of the value (the trait message), then field 2, field 2, field 1, value 4. The paths in the tables below are written the same way, starting from the trait message (so the leading `1.` is dropped).

## Trait Reference

| Trait  | Meaning                    | Status    | Fields (from the trait message)                                                                                                            |
| ------ | -------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `1/2`  | Gateway clock or boot      | Guess     | `3.1.1`, `4.1.1` epoch ms a few days old (a reboot or update?); `5` 125 / 224, unknown                                                     |
| `1/4`  | Cellular interface         | Confirmed | network interface (see below); also `2` IMEI, `10` ICCID, `5` carrier, `4`=5, `7`=4, `9`=2 (signal level is one of these)                  |
| `1/5`  | Ethernet interface         | Confirmed | network interface (see below)                                                                                                              |
| `1/6`  | Wi-Fi interface            | Confirmed | network interface (see below); `2` active access point: `4` frequency MHz, `5`=10 (signal?)                                                |
| `1/7`  | Interface order            | Strong    | `1` TraitRefs to `5/5`, `5/6`, `5/4` (Ethernet, Wi-Fi, cellular); `2` the interface in use (Ethernet on both)                              |
| `1/12` | Panel                      | Strong    | `17.1` size (16 or 40); `22.1.2` feeder rating in mA; `34` install record (year, model codes)                                              |
| `1/14` | Site                       | Strong    | location as doubles (withheld); `11.1` epoch ms the site was set up; `23.2.2.1` latest report time                                         |
| `1/15` | Circuit position           | Confirmed | TraitRefs to the circuit's `1/16` label and `1/17` spaces, under field 11 or 13                                                            |
| `1/16` | Circuit label              | Confirmed | `1` load type (1 lights, 2 outlets, 3 appliance?); `2` voltage (1 = 120 V, 3 = 240 V); `3` breaker amps; `4` name                          |
| `1/17` | Space                      | Confirmed | `3` the space number as printed on the panel                                                                                               |
| `1/18` | Battery                    | Strong    | Tesla; `4.1.1` 13500 Wh and `4.11` 11500 W (one Powerwall 3; the expansion unit is not counted); `3.1.2.1` latest contact?                 |
| `1/20` | Breaker module             | Strong    | eight-space modules, model `1-02469-04`                                                                                                    |
| `1/21` | Feeder                     | Confirmed | `2` kind (3 = main breaker, 4 = feed-through lugs); `4` and `5` rating in A                                                                |
| `1/26` | Wiring leg                 | Strong    | `1.11`: 1 or 2 = leg, 4 = two-pole                                                                                                         |
| `1/31` | Switch and load management | Confirmed | see below                                                                                                                                  |
| `1/32` | PowerUp limits             | Confirmed | `2.1` limit in mA; `5` role (1 = service limit, 4 = battery limit); `11`..`14` stage thresholds, not decoded                               |
| `1/38` | Battery shed threshold     | Strong    | `1.1` 13500 Wh; `7` 500 / 490 / 510 in tenths of a percent: the 50% threshold of "stay on until 50% battery", with 49% and 51% either side |
| `1/40` | Battery alerts             | Confirmed | `11` one per alert: `1` id, `2` severity?, `3.1` epoch ms; the kind is which empty field follows (`7` "issue", `6` "connection lost")      |
| `1/45` | Metering                   | Guess     | `2` meter accuracy?, `3` expected source meters?                                                                                           |
| `1/46` | DER backup                 | Guess     | `1` backup state, `2` SPAN MID used                                                                                                        |
| `5/1`  | Device description         | Strong    | `4` serial, `5` firmware, `7` supported traits, `9` part number                                                                            |

### Trait 1/31: Switch and Load Management

```text
1: TraitRef back to the 1/15 circuit (check it: a mismatch means the entry is not this circuit's)
2: config {
     1:  shed policy (1 always on: the app setting, and the battery breaker; 2 every other)
     2:  backup { 1: backup priority }
     3:  { 2: minimum current, mA }
     18: minimum reconnect, s
     19: minimum disconnect, s
     21: disconnect timeout, s
     22: PowerUp rank (the default disconnect rank)
   }
3: switch state (1 unknown, 2 open, 3 closed)
```

**Backup priority** (`2.2.1`, confirmed against the app's Battery priority screen on the MAIN 40):

| Value  | SPAN app label                 | Official `CircuitPriority` |
| ------ | ------------------------------ | -------------------------- |
| 4      | Stay on in an outage           | `NEVER`                    |
| 3      | Stay on until 50% battery      | `SOC_THRESHOLD`            |
| 2      | Not available in an outage     | `OFF_GRID`                 |
| absent | (no backup block in the entry) | `UNKNOWN`                  |

**PowerUp rank** (`2.22`, confirmed): sorting by rank ascending reproduces the app's PowerUp control-action list on both panels. Equal ranks are a tie
the app breaks by breaker rating, highest first, then by space. A circuit with no rank is listed last.

**Always on** (strong): shed policy `1`. Exactly one breaker was set to always on in the app, on the MAIN 16, with none on the MAIN 40 as a
control; exactly one user circuit has policy `1`, on the MAIN 16 (space 24). Its only other appearance is the Powerwall's own breaker, which SPAN
never turns off. To confirm outright: set one MAIN 40 breaker to always on, re-run the inventory, expect its `2.1` to read `1`, then set it back.

### Traits 1/4, 1/5, 1/6: Network Interfaces

The three share one layout:

```text
1: interface {
     1: name   2: MAC   3: type (2 Ethernet, 3 Wi-Fi, 9 cellular)   4: MTU
     5: status { 1: state (7 connected, 3 down), 2: { 1: substate, 2: reason } }
     6, 7: IPv4 and IPv6 configuration, including routes with their metrics
   }
```

The interfaces were told apart by their default route metrics, 100, 600 and 700, which are NetworkManager's defaults for Ethernet, Wi-Fi and
mobile broadband. The app agrees: Wi-Fi connected on the MAIN 40 (2412 MHz) and down on the MAIN 16, cellular up on both.

### Trait 1/40: Battery Alerts

The app's System alerts for the battery are here, not only behind `ListDispatches`. Two entries, 40 seconds apart, carry the timestamps of the two
alerts the app lists on 12 May 2026: "Tesla Powerwall 3 issue" at 4:19 PM and "Tesla Powerwall 3 connection lost" at 4:20 PM.

### Trait 1/32: PowerUp Limits

Role 1 is the service limit PowerUp holds the panel to: 150 A on the MAIN 16 and 200 A on the MAIN 40, as the app's PowerUp screen shows. Role 4
appears only on the site with the Powerwall 3, at 47917 mA. That is 11.5 kW at 240 V, the Powerwall 3's maximum continuous output and
48 A continuous current on Tesla's datasheet, so it is read as the limit while the battery supplies the house.

## Telemetry Frames

Decoded by `cloud_telemetry.py`; field layout in `CLOUD-PROTO.md`. Each sample is identified by its trait instance id, which matches the snapshot's `1/15` instance for branch
circuits. Non-circuit instances: the main feed is the lowest-numbered lugs instance (2), and a MAIN 40 also reports its feed-through lugs (41). The
panel sample's field 2 is the feed-through power and current, which the app shows as "Feed-thru lugs".

Site flows: `grid` is positive while importing. Solar and battery signs are not yet checked against a site that has them running.

## Energy History

`GetHistoryAggregation` returns SPAN's metered kWh per bucket. Request and response layout, and the three details that are easy to get wrong
(bare-varint metric id, milliseconds, telemetry instance ids), are in the docstring of `span_client/cloud_history.py`. Site components:
`grid_import`, `grid_export`, `home_import`, `home_export`, `battery_import`, `battery_export`, `solar_to_grid`, `solar_to_home`,
`solar_to_battery`, `battery_to_home`, `grid_to_home`. Circuits report `import` and `export`.

The app's e-mailed export is the same data at hourly resolution:

- One file per panel. Columns: local time, then one column per circuit ordered by telemetry instance id (names can repeat, so order is the only
  key), then the eleven site components above.
- Times are local wall-clock with no offset. On the autumn DST change the 01:00 row appears twice: the first is daylight time, the second
  standard time.
- The file is named for the whole month but stops at the last complete hour.
- `home_import` equals the sum of the circuits to within 0.2%. `grid_import` exceeds it by about 9 W around the clock on a MAIN 16, which is
  the panel's own consumption.

## Integration Mapping

How the official integration's cloud transport fills its snapshot (`cloud/snapshot.py` in vic-georgiev/span). Configuration is re-read with each snapshot refresh and published as
readings, so a change made in the app reaches Home Assistant without a reload.

| Snapshot field                                  | Source                     |
| ----------------------------------------------- | -------------------------- |
| `circuit.priority`, `is_sheddable`              | `1/31` backup priority     |
| `circuit.pcs_priority`                          | `1/31` PowerUp rank        |
| `circuit.breaker_rating_a`, `tabs`              | `1/16` amps, `1/17` spaces |
| `main_breaker_rating_a`                         | `1/21` main breaker        |
| `pcs.import_limit_a`, `pcs.feed_import_limit_a` | `1/32` role 1              |
| `pcs.off_grid_import_limit_a`                   | `1/32` role 4              |
| `feedthrough_power_w`                           | panel sample field 2       |
| `eth0_link`, `wlan_link`, `wwan_link`           | `1/5`, `1/6`, `1/4` state  |
| `circuit.always_on` (and no switch when set)    | `1/31` shed policy 1       |

Still unresolved: never-backup, relay requester, produced energy, door, uptime, whether PowerUp is limiting now.
Priority can be read but not written over the cloud, so no priority select is offered.

## Open Questions

- The always-on field (see above).
- Cellular signal level: `1/4` fields `4`, `7`, `9` are the candidates; both panels read "Good", so a change is needed to tell them apart.
- The `ListDispatches` request shape, for alerts other than the battery's.
- Local access. Both panels are wired to SPAN's Network Kit, a MikroTik hEX S inside the MAIN 40, which routes them (and the Powerwall) to the home
  network with NAT, so they cannot be reached from the home LAN through it. The MAIN 40 is also on the home Wi-Fi, which is how it was reached for
  `FINDINGS.md`; the MAIN 16 is not. SPAN's local API is live on the MAIN 32 only; for the MAIN 16 and MAIN 40 it was announced for later in 2026.
- The grid-status override the app offers ("Assumed grid status"), and where it lives.
- Battery capacity: the snapshot says 13.5 kWh for a Powerwall 3 with an expansion unit. Check again after commissioning.
