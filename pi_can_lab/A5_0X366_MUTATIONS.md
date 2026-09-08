# Audi A5 0x366 Targeted Mutation

## 목적과 호환성

`a5_0x366_mutator.py`는 Audi A5 2020의 `Blinkmodi_02` 전용 DBC-aware mutation을 생성한다. 기존 `mutation_engine.py`의 bit/byte/random 로직과 `strategy_selector.py`의 boundary/neighbor feedback 로직은 삭제하거나 대체하지 않았다. `--mutation-profile`을 생략하면 기존 경로가 그대로 사용된다.

기준 정보:

- CAN ID: `0x366`
- Message: `Blinkmodi_02`
- DLC: 8
- DBC cycle time: 1000ms
- Baseline: `00000000200000F0`
- 모든 signal byte order: Intel/little-endian

DBC message attribute에서 직접 읽은 timing은 normal cycle 1000ms, fast cycle 50ms,
delay 10ms, repetition 5이다. Temporal case는 10/50/100/500/1000ms 간격을 생성하며 각
mutation metadata에 원본 DBC timing도 함께 기록한다.

## DBC occupancy

실제 `A5.dbc`에서 계산한 defined bit는 `12..44`, `59..63`이며 undefined bit는 `0..11`, `45..58`이다.

| Signal | Start | Length | Occupied global bits |
|---|---:|---:|---|
| BM_ZV_auf | 12 | 1 | 12 |
| BM_ZV_zu | 13 | 1 | 13 |
| BM_DWA_ein | 14 | 1 | 14 |
| BM_DWA_Alarm | 15 | 1 | 15 |
| BM_Crash | 16 | 1 | 16 |
| BM_Panik | 17 | 1 | 17 |
| BM_Not_Bremsung | 18 | 1 | 18 |
| BM_GDO | 19 | 1 | 19 |
| BM_Warnblinken | 20 | 1 | 20 |
| BM_Taxi_Notalarm | 21 | 1 | 21 |
| BM_Telematik | 22 | 1 | 22 |
| BM_links | 23 | 1 | 23 |
| BM_rechts | 24 | 1 | 24 |
| Blinken_li_Fzg_Takt | 25 | 1 | 25 |
| Blinken_re_Fzg_Takt | 26 | 1 | 26 |
| Blinken_li_Kombi_Takt | 27 | 1 | 27 |
| Blinken_re_Kombi_Takt | 28 | 1 | 28 |
| BM_NBA_n_codiert_n_aktiv | 29 | 1 | 29 |
| BM_NBA_Status | 30 | 2 | 30..31 |
| BM_WBT_Beleuchtung | 32 | 1 | 32 |
| BM_HD_Oeffnung_angelernt | 33 | 1 | 33 |
| BM_Autobahn | 34 | 1 | 34 |
| BM_Rollenmodus_Blinken | 35 | 1 | 35 |
| BM_Recas | 36 | 1 | 36 |
| BM_Wischblinken | 37 | 1 | 37 |
| BM_Telematik_Abbruchgrund | 38 | 6 | 38..43 |
| BM_PiloPa | 44 | 1 | 44 |
| DWA_Alarmquelle | 59 | 5 | 59..63 |

전체 64-bit map은 다음 명령으로 언제든 실제 DBC에서 다시 출력한다.

```bash
python3 experiment_runner.py \
  --config experiment_runner.yaml \
  --print-0x366-map
```

## Undefined enum

raw 값은 `scaling=False`로 처리해 physical scale/offset과 혼동하지 않는다. 이 메시지의 세 multi-bit signal은 모두 unsigned, scale 1, offset 0이다.

| Signal | Bits | Possible raw | DBC-defined | Undefined |
|---|---:|---|---|---|
| BM_NBA_Status | 2 | 0..3 | 0, 1, 3 | 2 |
| BM_Telematik_Abbruchgrund | 6 | 0..63 | 0..15 | 16..63 |
| DWA_Alarmquelle | 5 | 0..31 | 0..25, 30, 31 | 26..29 |

## Mutation family와 profile

| Profile | Family |
|---|---|
| signal-aware | signal_single, signal_combination, state_contradiction, undefined_enum |
| undefined-only | undefined_bit_single, undefined_bit_multi |
| semantic-plus-undefined | defined_undefined_mix |
| temporal | temporal_sequence |
| all-0x366 | 모든 신규 family |

`undefined-only`는 생성 시 다음 조건을 강제 검증한다.

```python
assert decode_raw(mutated_payload) == decode_raw(base_payload)
assert mutated_payload != base_payload
```

Undefined bit에는 임의 의미를 붙이지 않고 `DBC_UNDEFINED_BIT_<global_bit>`로만 기록한다.

## 실행

기존 generic mutation:

```bash
python3 experiment_runner.py \
  --target-id 0x366 --source-bus B_CAN \
  --trials 20 --random-seed 366 --execute
```

Signal-aware:

```bash
python3 experiment_runner.py \
  --target-id 0x366 --source-bus B_CAN \
  --mutation-profile signal-aware \
  --trials 20 --random-seed 366 --execute
```

Undefined-only, 한 case에서 최대 2bit 변경:

```bash
python3 experiment_runner.py \
  --target-id 0x366 --source-bus B_CAN \
  --mutation-profile undefined-only \
  --undefined-max-bits 2 \
  --trials 20 --random-seed 366 --execute
```

전체 신규 family:

```bash
python3 experiment_runner.py \
  --target-id 0x366 --source-bus B_CAN \
  --mutation-profile all-0x366 \
  --undefined-max-bits 2 \
  --trials 100 --random-seed 366 --execute
```

## Manifest

기존 Trial 정수 ID는 FeedbackState 호환성을 위해 유지하고, `MUT-000001` 형식의 `mutation_uid`를 추가했다. `mutation.json`과 TX JSONL에는 다음 provenance가 기록된다.

- mutation ID/UID, timestamp, interface, CAN ID, payload
- mutation profile, family, case
- base/mutated payload
- signals changed: signal, before, after, start, length
- undefined bits changed
- undefined enum signal/raw value
- temporal sequence name, frames, interval
- parent mutation과 generation reason

다른 버스에서 동일한 mutated `0x366`이 보이는 현상은 `propagated_payloads` transport evidence로만 저장한다. 이 routing 자체는 interesting anomaly에 포함하지 않으며, 다른 CAN ID의 신규 등장·소실·payload·timing 변화가 Feedback 대상이다.

## Payload sample

모든 sample의 base는 `00000000200000F0`이다. 실제 mutation ID는 Trial 선택 순서에 따라 `MUT-xxxxxx`로 부여된다.

### signal mutation

| Case | Change | Payload |
|---|---|---|
| BM_ZV_auf_RAW_1 | BM_ZV_auf 0→1 | `00100000200000F0` |
| BM_ZV_zu_RAW_1 | BM_ZV_zu 0→1 | `00200000200000F0` |
| BM_DWA_ein_RAW_1 | BM_DWA_ein 0→1 | `00400000200000F0` |
| BM_DWA_Alarm_RAW_1 | BM_DWA_Alarm 0→1 | `00800000200000F0` |
| BM_Crash_RAW_1 | BM_Crash 0→1 | `00000100200000F0` |

### undefined enum

| Case | Payload |
|---|---|
| BM_NBA_Status_UNDEFINED_RAW_2 | `00000080200000F0` |
| BM_Telematik_Abbruchgrund_UNDEFINED_RAW_16 | `00000000200400F0` |
| BM_Telematik_Abbruchgrund_UNDEFINED_RAW_17 | `00000000600400F0` |
| BM_Telematik_Abbruchgrund_UNDEFINED_RAW_18 | `00000000A00400F0` |
| BM_Telematik_Abbruchgrund_UNDEFINED_RAW_19 | `00000000E00400F0` |

### undefined bit

| Case | Payload | Defined signal change |
|---|---|---|
| DBC_UNDEFINED_BIT_0 | `01000000200000F0` | none |
| DBC_UNDEFINED_BIT_1 | `02000000200000F0` | none |
| DBC_UNDEFINED_BIT_2 | `04000000200000F0` | none |
| DBC_UNDEFINED_BIT_3 | `08000000200000F0` | none |
| DBC_UNDEFINED_BIT_4 | `10000000200000F0` | none |

### defined + undefined

| Case | Payload |
|---|---|
| BM_Crash + bit 0 | `01000100200000F0` |
| BM_Crash + bit 6 | `40000100200000F0` |
| BM_Crash + bit 11 | `00080100200000F0` |
| BM_Crash + bit 45 | `00000100202000F0` |
| BM_Crash + bit 52 | `00000100200010F0` |
