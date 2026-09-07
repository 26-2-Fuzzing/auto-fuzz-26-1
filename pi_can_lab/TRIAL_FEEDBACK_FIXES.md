# Trial-based Feedback Pipeline 안정성 수정

## 배경

Trial 기반 feedback pipeline은 다음 순서로 동작합니다.

```text
원본 payload 확인
→ mutation 선택
→ P-CAN/B-CAN/I-CAN 캡처 시작
→ baseline 구간
→ normal payload 송신
→ mutation 반복 송신
→ 원본 payload 복원
→ recovery 구간
→ 세 Pi의 로그 다운로드
→ anomaly 분석
→ feedback.json 생성
→ feedback_state.json 갱신
→ 다음 Trial 전략 선택
```

정상 실행 경로에 대한 기존 테스트는 통과했지만, 실제 장비에서는 다음과 같은 예외 상황도 안전하게 처리해야 합니다.

- Raw CAN 송신 후 원본 payload가 복원되지 않는 경우
- 세 Pi 중 일부만 receiver 실행에 성공하는 경우
- baseline timing jitter가 정확히 0인 경우
- 캡처 시작 전 준비 과정에서 실패하는 경우
- FeedbackState 갱신 도중 프로세스가 종료되는 경우

이 문제들을 해결하기 위해 총 5가지 안정성 수정을 적용했습니다.

---

## 1. Raw Trial 종료 후 원본 payload 복원

### 문제

`sender_trial.yaml`에는 다음과 같이 원본 payload 복원이 활성화되어 있습니다.

```yaml
transmit:
  restore_original: true
  restore_count: 1
  restore_delay_ms: 100.0
```

하지만 Trial runner는 CAN ID와 payload를 DBC 방식이 아닌 raw 방식으로 전달합니다.

```text
--id 0x366
--data 00000000200000F0
--mutation-data 00000020200000F0
```

기존 `can_sender.py`는 DBC 모드에서만 설정되는 `base_payload`가 존재할 때만 복원했습니다.

```python
if restore and base_payload is not None:
    ...
```

Raw 모드에서는 `base_payload`가 `None`이므로 `restore_original: true`여도 실제 복원 프레임이 송신되지 않았습니다.

### 영향

Mutation 반복 송신이 끝난 후 마지막으로 송신된 변형 payload가 버스 또는 대상 ECU 상태에 영향을 줄 수 있습니다.

Recovery 구간이 존재해도 원본 payload를 명시적으로 한 번 복원하지 않기 때문에 설정과 실제 동작이 일치하지 않았습니다.

### 수정 내용

Campaign에서 사용하는 원본 payload인 `normal_payload`를 복원 payload로 사용하도록 변경했습니다.

```python
restore_payload = normal_payload
should_restore = restore and (
    base_payload is not None or campaign_enabled
)
```

복원 프레임 생성, JSONL 기록 및 콘솔 출력도 모두 `restore_payload`를 사용합니다.

```python
message = create_message(
    frame_id,
    restore_payload,
    is_extended,
    is_fd,
    bitrate_switch,
)
```

일반적인 비-campaign raw 송신에는 예상하지 않은 추가 프레임이 생기지 않도록, raw 복원은 campaign이 활성화된 경우에만 적용합니다.

### 변경 후 송신 순서

```text
normal   → 원본 payload
mutation → 변형 payload 반복 송신
restore  → 원본 payload
recovery → passive 관찰
```

Preview 로그에서도 다음 순서를 확인했습니다.

```text
normal    00000000200000F0
mutation  00000020200000F0
restore   00000000200000F0
```

---

## 2. 일부 Pi의 캡처 시작 실패 시 나머지 receiver 정리

### 문제

P-CAN, B-CAN, I-CAN receiver는 병렬로 시작됩니다.

기존 구현은 한 future에서 예외가 발생하면 그 순간 이미 완료된 future만 찾아서 종료했습니다.

예를 들어 다음 순서로 실행될 수 있습니다.

```text
P-CAN → 즉시 시작 실패
B-CAN → 아직 시작 중
I-CAN → 아직 시작 중
```

P-CAN 실패를 처리하는 시점에는 B-CAN과 I-CAN future가 완료되지 않았기 때문에 cleanup 대상에 포함되지 않습니다.

그 후 B-CAN과 I-CAN receiver가 성공적으로 시작되면 해당 프로세스는 종료되지 않고 Raspberry Pi에 남을 수 있습니다.

### 영향

남은 receiver 프로세스가 다음 문제를 일으킬 수 있습니다.

- 불필요한 CAN 로그 계속 기록
- 원격 저장 공간 사용
- 다음 Trial에서 동일 출력 경로 충돌
- `output_policy: fail`로 다음 Trial 시작 실패
- 실험자가 모든 캡처가 중단됐다고 잘못 판단

### 수정 내용

`as_completed()`를 사용해 모든 future의 결과를 끝까지 수집하도록 변경했습니다.

```python
for future in as_completed(futures):
    bus = futures[future]
    try:
        started[bus] = future.result()
    except Exception as exc:
        errors.append(f"{bus}: {exc}")
```

하나 이상의 시작 오류가 있으면 성공적으로 시작된 모든 receiver를 종료합니다.

```python
if errors:
    for bus, handle in started.items():
        self.managers[bus].stop_process(handle.process)
```

Receiver 종료 과정에서 추가 오류가 발생하면 최초 시작 오류와 cleanup 오류를 함께 보고합니다.

### 변경 후 동작

```text
세 Pi 모두 성공
→ 캡처 진행

하나 이상 실패
→ 나머지 future 완료 대기
→ 성공적으로 시작된 receiver 모두 종료
→ Trial 실패 처리
```

---

## 3. Baseline 표준편차가 0일 때 timing jitter 탐지

### 문제

Timing anomaly는 baseline과 mutation 구간의 다음 통계를 비교합니다.

- 평균 cycle time
- 중앙값 cycle time
- cycle time 표준편차

기존 상대 변화율 함수는 baseline 값이 0이면 비교 결과를 반환하지 않았습니다.

```python
if before is None or after is None or before <= 0:
    return None
```

예를 들어 baseline 프레임이 정확히 20ms 간격으로 수신되면 표준편차가 0ms입니다.

Mutation 이후 간격이 다음과 같이 바뀌어도 평균과 중앙값은 여전히 20ms일 수 있습니다.

```text
Baseline: 20, 20, 20, 20ms
Mutation: 10, 30, 10, 30ms
```

이 경우:

```text
Baseline stddev: 0ms
Mutation stddev: 10ms
```

하지만 기존 구현은 baseline 표준편차가 0이기 때문에 변화를 무시했습니다.

### 영향

평균 송신 주기는 유지되지만 프레임 간격이 불안정해지는 timing jitter를 놓칠 수 있습니다.

CAN 통신에서는 평균 주기가 동일하더라도 큰 jitter가 제어기 동작이나 timeout 판정에 영향을 줄 수 있습니다.

### 수정 내용

평균과 중앙값은 기존 상대 변화율을 사용하고, 표준편차는 절대 증가량을 추가로 사용하도록 분리했습니다.

새 설정:

```yaml
anomaly_thresholds:
  timing_relative_change: 0.25
  timing_stddev_absolute_ms: 2.0
```

표준편차 절대 증가량:

```python
stddev_increase = max(
    0.0,
    mutation_stddev - baseline_stddev,
)
```

TIMING anomaly 조건:

```text
평균 또는 중앙값의 상대 변화가 임계값 이상
또는
표준편차 절대 증가가 timing_stddev_absolute_ms 이상
```

Anomaly evidence에는 다음 값도 기록됩니다.

```json
{
  "baseline_stddev_ms": 0.0,
  "mutation_stddev_ms": 10.0,
  "stddev_absolute_increase_ms": 10.0
}
```

`Infinity` 같은 비표준 JSON 숫자를 만들지 않도록, 0으로 나누는 상대 변화율 대신 절대 임계값을 사용했습니다.

---

## 4. Trial 준비 단계 실패도 metadata에 기록

### 문제

기존 runner는 Trial 디렉터리를 먼저 생성한 후 다음 작업을 수행했습니다.

```text
원본 payload probe
→ reproduction mutation 검증
→ mutation 선택
→ DBC decode
→ clock offset 확인
→ metadata 생성
```

예외 처리는 캡처 시작 시점부터 적용됐기 때문에 그 이전 단계에서 실패하면 다음과 같은 불완전한 디렉터리가 남을 수 있었습니다.

```text
trial_0003/
```

또는 `mutation.json`만 존재하고 `metadata.json`이 없는 상태가 될 수 있었습니다.

### 영향

실패 원인을 나중에 확인하기 어렵고, 문서에 명시된 “실패한 Trial은 `failed` 상태로 남는다”는 동작과 일치하지 않았습니다.

### 수정 내용

Trial 디렉터리를 만든 직후 최소 metadata를 먼저 기록합니다.

```json
{
  "status": "preparing",
  "experiment_id": 42,
  "trial_id": 3,
  "target_id": "0x366",
  "source_bus": "B_CAN",
  "random_seed": 366,
  "start_time": "..."
}
```

다음 준비 작업 전체를 예외 처리 범위에 포함했습니다.

- Live payload probe
- Reproduction mutation 조회
- Source bus와 CAN ID 검증
- Baseline payload 일치 확인
- Mutation 선택
- DBC signal metadata 처리
- Clock offset 확인

준비 과정에서 실패하면 다음과 같이 기록합니다.

```json
{
  "status": "failed",
  "error": "RuntimeError: probe failed",
  "end_time": "..."
}
```

실패한 Trial은 FeedbackState에 반영되지 않으며, 다음 실행은 새로운 Trial 번호를 사용합니다.

---

## 5. FeedbackState 갱신을 멱등적이고 복구 가능하게 처리

### 문제

기존 완료 순서는 다음과 같았습니다.

```text
feedback.json 기록
→ feedback_state.json 갱신
→ metadata status=completed 기록
```

`feedback_state.json` 갱신 후 metadata 기록 전에 프로세스가 종료되거나 파일 기록이 실패하면 다음과 같은 불일치가 발생할 수 있습니다.

```text
feedback_state.json → Trial 반영됨
metadata.json       → captured 또는 failed
```

재실행 과정에서 같은 Trial을 다시 반영하면 통계가 중복 증가할 가능성도 있었습니다.

### 영향

다음 정보가 실제 완료 Trial 수와 달라질 수 있습니다.

- `total_trials`
- `mutation_history`
- `mutation_statistics.executed`
- `mutation_statistics.interesting`
- `interesting_mutations`

이 상태는 다음 mutation 전략 선택에도 영향을 줍니다.

### 수정 내용

완료 상태를 다음 순서로 변경했습니다.

```text
captured
→ analyzed
→ feedback_state 반영
→ completed
```

분석 결과와 feedback 파일이 모두 기록되면 metadata를 먼저 `analyzed`로 변경합니다.

```python
metadata["status"] = "analyzed"
```

FeedbackState에는 완료 처리된 Trial ID 목록을 저장합니다.

```json
{
  "completed_trial_ids": [1, 2, 4]
}
```

`record_completed_trial()`은 Trial ID와 mutation ID를 확인해 같은 Trial이 이미 반영됐다면 통계를 다시 증가시키지 않습니다.

```python
if (
    trial_id in completed_trial_ids
    or mutation_already_recorded
):
    return state
```

### 재시작 복구

Runner 시작 시 `status == "analyzed"`인 Trial을 검색합니다.

필요한 파일이 모두 존재하는 경우:

```text
metadata.json
mutation.json
feedback.json
```

다음 절차로 복구합니다.

```text
FeedbackState에 미반영
→ 멱등 방식으로 반영
→ metadata를 completed로 변경

FeedbackState에 이미 반영
→ 중복 반영하지 않음
→ metadata만 completed로 변경
```

따라서 어느 단계에서 프로세스가 종료되더라도 다음 실행에서 일관된 상태로 복구할 수 있습니다.

---

## Trial 상태 흐름

수정 후 Trial의 상태 변화는 다음과 같습니다.

```text
preparing
   │
   ├─ 준비 실패 ───────────────→ failed
   │
   ▼
prepared
   │
   ├─ 캡처/송신/다운로드 실패 ─→ failed
   │
   ▼
running
   │
   ▼
captured
   │
   ├─ 분석 실패 ───────────────→ failed
   │
   ▼
analyzed
   │
   ├─ 완료 처리 중단 ──────────→ 다음 실행에서 복구
   │
   ▼
completed
```

FeedbackState에는 completed Trial과 복구 가능한 analyzed Trial만 최종적으로 한 번 반영됩니다.

---

## 추가된 회귀 테스트

다음 테스트를 추가하거나 강화했습니다.

### Raw payload 복원

```text
test_campaign_preview_logs_all_phases_and_restores_raw_original
```

확인 항목:

- normal payload 송신
- mutation payload 송신
- recovery 직전 restore 송신
- restore data가 원본 payload와 동일

### 부분 캡처 시작 실패

```text
test_partial_start_failure_stops_late_successes
```

확인 항목:

- 한 Pi가 즉시 실패
- 다른 Pi가 지연 후 성공
- 성공한 receiver가 모두 종료됨
- capture handle이 남지 않음

### Zero-baseline jitter

```text
test_jitter_from_zero_baseline_stddev_is_detected
```

확인 항목:

- baseline 표준편차 0ms
- mutation 표준편차 10ms
- TIMING anomaly 생성

### 준비 단계 실패 기록

```text
test_preparation_failure_is_recorded_without_feedback
```

확인 항목:

- payload probe 실패
- metadata 상태가 failed
- 오류 메시지 기록
- FeedbackState의 total_trials가 증가하지 않음

### 완료 상태 복구

```text
test_analyzed_trial_reconciliation_is_idempotent
```

확인 항목:

- analyzed Trial 복구
- FeedbackState에 한 번만 반영
- 반복 복구 시 통계 중복 없음
- metadata가 completed로 전환됨
