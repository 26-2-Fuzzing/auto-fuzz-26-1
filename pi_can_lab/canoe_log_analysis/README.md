# CANoe 로그 오프라인 분석기

저장된 BLF/ASC에서 채널과 시간 구간별 수신 통신량, 메시지 주기 및 DBC 상태 전이를 비교한다. 버스에 연결하거나 프레임을 송신하지 않는다. 출력은 검토할 관측 후보이며 차량 이상, 입력 도달이나 인과관계를 자동 확정하지 않는다.

## 결과를 읽는 순서

JSON의 `assessment.headline_ko`를 먼저 읽는다. `candidates` 배열은 기존 도구와의 호환을 위해 유지되며, 그 길이는 **변화 규칙에 해당한 건수**이지 고장 횟수가 아니다. 같은 ID가 빈도와 주기 조건에 동시에 해당할 수도 있다.

| `assessment.status` | 이 결과로 말할 수 있는 것 |
|---|---|
| `descriptive_only` | 구간의 시작·끝을 뒷받침하는 기록이 없거나 비교에 쓸 수 있는 버스가 없다. 예를 들어 임의로 나눈 두 60초 구간에서 후보 27건이 나왔다면 “두 구간 사이에 변화 27건이 관찰됨 / 퍼징 영향 판정 불가”로 읽는다. |
| `window_review_only` | 실험 시간 근거가 분석 구간과 맞고 비교 가능한 버스가 하나 이상 있다. 해당 버스에서 **그 시간대에** 관찰된 변화를 검토할 수 있다. 이 상태는 입력이 ECU에 도달했다거나 차량 고장이 확인됐다는 뜻이 아니다. |

그다음 `quality`에서 **해당 버스**의 직전·관찰 구간이 모두 사용 가능한지 확인한다. DBC를 설정하지 않은 것, 감시 신호를 설정했지만 해당 표본이 없는 것, 표본을 읽었지만 전이가 검출되지 않은 것은 각각 다른 결과다. 대조 로그가 없거나 비교가 불가능한 경우도 “대조군에서 변화가 없었다”로 쓰면 안 된다.

## 기존 Pi 분석에서 옮긴 부분

기준은 [pi_can_lab의 trial_analysis.py](https://github.com/26-2-Fuzzing/auto-fuzz-26-1/blob/c7589f18c668bc7619596dac9801768e099c8467/pi_can_lab/trial_analysis.py#L326-L351)와 [analyze_fuzz_response.py의 구간별 집계](https://github.com/26-2-Fuzzing/auto-fuzz-26-1/blob/c7589f18c668bc7619596dac9801768e099c8467/pi_can_lab/analyze_fuzz_response.py#L178-L191)다. 저장 포맷과 상관없는 기술 통계·관측 비교를 독립 모듈로 구현했다. 기존 실행기, 송신 기록과의 대조 또는 다음 입력 선정에 의존하지 않으며, Pi 보고서를 그대로 재현하는 호환 모드는 아니다.

| 파일 | 역할 |
|---|---|
| `canoe_reader.py` | BLF·ASC를 채널·시각·ID·RX/TX를 가진 공통 프레임으로 읽고, 지정한 DBC 신호만 디코딩한다. |
| `canoe_metrics.py` | 버스·ID·표준/확장·Classic/FD·구간별 개수, Hz, 주기 평균·중앙값·최소·최대·모표준편차, payload 고유값 개수와 연속 변화 개수를 집계한다. |
| `analyze_canoe.py` | 품질, 이전 기준 구간과의 비교, 관측 후보, 복구 및 별도 대조 기록을 묶어 JSON으로 저장한다. |

기존 `count`, `hz`, `jitter_ms` 키는 유지했다. `jitter_ms`는 이전 CANoe 코드의 표본표준편차이며, Pi와 직접 비교할 때는 모표준편차인 `cycle_time_stddev_ms`를 사용한다. 정확한 중앙값 또는 고유값 수의 저장 한도(구간·ID별 4,096개)를 넘으면 해당 값을 `null`로 남기고 절단을 표시한다. 원본 payload 자체를 결과에 복사하지 않으며, 값이 여러 번 바뀌었다는 사실만으로 이상 후보 점수를 만들지 않는다.

## 입력 준비

1. CANoe에서 측정을 종료하고 닫힌 BLF 또는 ASC 파일을 준비한다. CANoe Offline 화면에서 채널 번호와 동일한 몇 개 프레임의 ID·데이터·간격을 먼저 확인한다.
2. `example_config.json`을 복사해 실제 채널 대응표와 분석 구간을 넣는다. 예제의 채널 번호와 버스 이름은 예시다. `channel_map`의 키는 CANoe 화면 번호가 아니라 Python 리더가 반환한 값이다. 매핑되지 않은 번호는 결과의 `unmapped_channel_frames`에서 확인할 수 있다.
3. `timestamp_origin`과 각 구간을 실제 기록의 같은 시간 기준에서 정한다. 분석 시각은 `reader_timestamp - timestamp_origin`이다. ASC는 상대 시각 모드로 읽고, BLF 리더는 일반적으로 기록 시작 시각이 합쳐진 Unix 초를 반환한다. 별도 시험 기록과 시간을 맞출 근거가 없다면 구간을 추측하지 않는다. 각 구간은 [시작, 끝)이다.
4. `capture_loss_reported`는 CANoe 기록이나 측정 기록을 확인한 값이다. 모르면 `null`이다. BLF 리더가 모든 비프레임 이벤트를 노출하는 것은 아니므로 유실 경고가 JSON에 없다고 무유실이 증명되지는 않는다.
5. DBC 상태를 보려면 예제 설정의 `watch_signals`에 예를 들어 `{"bus":"CAN_A","message":"Status","signal":"FaultFlag"}`를 추가하고 `--dbc`를 준다. 상태·플래그 중심으로 선정한다. 카운터·체크섬·연속 센서값을 상태 신호처럼 등록하면 정상 변화도 후보가 된다.
6. 확보한 기록에 앞선 기준 구간이 있다면 `example_config_with_history.json`처럼 `baseline`과 `previous_reference`를 추가할 수 있다. `previous_reference`는 `reference` 바로 앞의 같은 길이 구간이다. 두 구간 사이에서 빈도나 중앙 주기가 이미 크게 변했다면 이후의 빈도·주기·소실 후보는 `reference_transitioning`으로 `observations`에만 남긴다. 해당 구간이 부족하면 `reference_unmeasured`이다. 구간이 없으면 기존처럼 `reference`와 `observation`만 비교한다.

### 실험 시각 근거를 넣는 방법

시간 통계만 검산할 때는 `windows`만 지정하면 된다. 이를 실험 시간 근거로 취급하지 않는다. ASC에서는 `FuzzSchedule::StartNs`·`EndNs` 등 지원되는 기록에서 시작·종료를 짝지어 설정한 관찰 구간과 대조한다. 초기값 0, READY 표시, 프레임 수 또는 파일 이름만으로는 구간을 인정하지 않는다. 이 분석기의 BLF 프레임 리더는 해당 시스템 변수 마커를 확인하지 않는다. **BLF에서 마커를 읽지 못했다는 것이 마커가 기록되지 않았다는 뜻은 아니다.**

CANoe 마커 대신 별도의 시각 기록을 사용하려면, 측정 시간축에 맞춘 뒤 아래처럼 설정에 `window_evidence`를 추가한다. 예시는 실제로 측정일지에 시작 4초, 종료 8초가 기록되어 있고, `windows.observation`도 `[4, 8]`일 때만 해당한다.

```json
{
  "window_evidence": {
    "source": "operator_record",
    "description": "동일 측정의 관찰 일지 3번. CANoe 측정 경과시간 기준 시작 4초, 종료 8초.",
    "observation_s": [4, 8]
  }
}
```

실제 일지에 있는 식별 정보와 시각을 적는다. 분석기는 설정한 시각과의 일치 여부를 확인할 뿐 일지 자체를 인증하지 않는다. 근거가 없으면 이 항목을 빼고 나온 통계만 보고한다.

자동 대조는 닫힌 단일 ASC 트리거 블록의 절대 경과시간 형식에서만 지원한다. 지원되는 시작·종료 변수의 값과 해당 이벤트 기록 시각, 설정한 구간의 양 끝이 각각 1ms 안에서 맞아야 한다. 다른 마커 형식, 미리 기록한 예정 시각, 누락되거나 충돌하는 마커는 실험 시각으로 추측하지 않는다. 기준 구간이 다른 기록된 실험 구간과 겹쳐도 승격하지 않는다. 이 검사는 기록된 스케줄 시각에 관한 것으로 실제 송신 성공 증거가 아니다.

## 실행

```bash
python3 -m pip install -r requirements.txt
python3 analyze_canoe.py capture.blf capture_config.json --out report.json
```

대조 녹화가 있다면 아래처럼 별도 파일과 시간 기준을 함께 지정한다. **실험과 같은 절차로 입력 변경 없이 녹화한 No-op**이면 대조 설정(`control_config.json`)에 `"capture_role": "no_op"`를 추가한다. 단순 배경 로그이거나 No-op인지 모르면 이 항목을 빼고 일반 대조 파일로 비교한다. 이미 있는 결과 파일은 덮어쓰지 않는다.

```bash
python3 analyze_canoe.py capture.blf capture_config.json --dbc signals.dbc \
  --control-log control.blf --control-config control_config.json --out report_with_control.json
```

## 판정 규칙과 한계

| 단계 | 확인하는 내용 |
|---|---|
| 수집 품질 | 버스별·구간별 RX 존재와 시작/끝 부근 수집 여부, 오류 프레임, 방향 불명, 역순 시각, 별도로 확인한 유실 여부. 불충분한 버스/구간은 `inconclusive`이고 후보 집계에서 제외한다. `usable`은 관측이 구간을 덮었다는 뜻으로, 프레임 유실이 없다는 증거는 아니다. |
| 신규·소실 | 기준 구간에 없고 관측 구간에 3건 이상이면 구간 내 신규 후보. 앞선 `baseline` 또는 `previous_reference`에서 이미 보이면 배경 관측으로 남긴다. 기준 구간 5건 이상, 관측 구간 기대치 5건 이상인데 0건이어야 소실 후보. 저빈도 메시지에 짧은 창을 썼다면 '소실 없음'이 아니라 이 기준으로는 판정하지 않는다는 뜻이다. |
| 빈도·주기 | 길이가 다른 구간은 초당 빈도로 비교한다. 기대치와 3프레임 이상 및 50% 이상 차이가 나야 빈도 후보로 기록한다. 간격이 각 구간에 3개 이상일 때 평균/중앙값 변화(2ms 이상 및 25% 이상)를 비교하고, 기존 CANoe의 간격 표본 5개 이상에서의 표준편차 비교는 그대로 유지한다. |
| DBC 상태 | 신호의 처음과 마지막 관측값, 관측 횟수와 전이 시간 범위를 남긴다. 바뀌기 전후의 샘플이 같은 관측 구간 안에 있어야 전이 후보로 분류한다. 경계에 걸리거나 긴 공백을 사이에 둔 전이는 근거 목록에만 남긴다. 기준값이 안정적이면 복구 구간의 실제 표본 중 몇 개가 기준값과 맞았는지 기록한다. 샘플 사이에도 그 상태가 계속됐는지는 자동 확정하지 않는다. |
| 대조 녹화 | 같은 버스의 비교 구간 길이와 수집 품질을 확인해 대조에서도 관측되는지 보여준다. 신호는 초기값도 비교한다. 한 번의 대조에서 플래그가 없다는 사실만으로 정상/이상이나 영향을 단정하지 않는다. |

위 수치는 **초기 선별 기준**이다. 실제 반복 로그의 배경 변동을 정량화하기 전에는 확정 임계값이 아니다. `new_id`는 이번 기준창에 없었다는 뜻으로, 과거에 한 번도 없었던 ID라는 의미가 아니다. B/P/I에서 보고된 후보는 bus별 관측 위치를 나타내며 서로 독립된 사건이거나 게이트웨이가 전달했다는 인과 결론을 내리지 않는다.

`per_id`에는 구간별 측정값이, `signals`에는 지정 신호의 샘플 수와 전이 목록이 들어 있다. TX 표시만으로 독립 수신이나 ECU 처리 여부를 확인할 수 없으므로 입력 도달은 출력에서 확정하지 않는다.

`--control-log`는 사람이 대조 실험으로 지정한 완료 파일을 받는다. 도구는 차량 상태나 실제 입력 조건이 동일했는지 확인할 수 없다.

각 후보의 `control.scope`가 `descriptive_windows_only`라면 비교한 두 구간 중 하나 이상에 실험 시각 근거가 없다. 대조 로그 표본이 부족하거나 대조 기준이 이미 흔들리고 있으면 해당 비교는 `control_not_comparable`로 남는다. `assessment.dbc.transition_candidate_count`는 관찰 전이를 평가할 수 없으면 `null`, 비교할 표본이 있지만 전이 후보가 없을 때는 `0`이다. `partially_observed`면 이 수는 비교 가능한 감시 신호에만 해당한다.

참고: [python-can 파일 읽기](https://python-can.readthedocs.io/en/stable/file_io.html#can.LogReader), [cantools DBC 디코딩](https://cantools.readthedocs.io/en/latest/).

```bash
python3 -m unittest -v
```
