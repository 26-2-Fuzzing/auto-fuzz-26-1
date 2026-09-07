# CAN 퍼징 로그·보고서 운영 기준

## 생성 파일

수신기는 실행할 때마다 기존 로그에 append하지 않고 다음 번호를 원자적으로 예약합니다.

```text
logs/b_can_1.jsonl   logs/b_can_1.md
logs/b_can_2.jsonl   logs/b_can_2.md
logs/i_can_1.jsonl   logs/i_can_1.md
logs/p_can_1.jsonl   logs/p_can_1.md
```

`*.jsonl`은 재분석 가능한 원본이고, 같은 이름의 `*.md`는 사람이 바로 읽는 회차 요약입니다.
송신 manifest도 `b_can_tx_1.jsonl`, `b_can_tx_2.jsonl` 순서로 새로 생성됩니다.

수신 보고서는 정상 종료 또는 Ctrl+C 시 자동 생성되며 다음을 보여 줍니다.

- 캡처 시간, frame rate, ID 수, DBC decode 상태
- watch 신호의 이전 값/변경 값/시각/payload
- SocketCAN 오류와 RX/TX overflow, bus-off 등 해석
- 트래픽 상위 ID, 실제 관찰 주기, unique payload 수
- `특이사항 없음`, `Watch 신호 변화`, `CAN 오류`, `데이터 불충분` 요약

## 한 회차 실행

여러 장비에서 같은 문자열을 `experiment_id`로 사용하십시오. 파일 번호는 장비별로 다를 수
있으므로 번호만으로 B/I/P 로그를 짝지으면 안 됩니다.

```bash
EXPERIMENT_ID=hazard_001

# B-CAN 수신 장비
python3 pi_can_lab/can_receiver.py \
  --config pi_can_lab/receiver_b_can.yaml \
  --experiment-id "$EXPERIMENT_ID"

# I-CAN 수신 장비
python3 pi_can_lab/can_receiver.py \
  --config pi_can_lab/receiver_i_can.yaml \
  --experiment-id "$EXPERIMENT_ID"

# P-CAN 수신 장비
python3 pi_can_lab/can_receiver.py \
  --config pi_can_lab/receiver_p_can.yaml \
  --experiment-id "$EXPERIMENT_ID"

# B-CAN 송신 장비(반드시 preview 확인 후 격리된 벤치에서만 실행)
python3 pi_can_lab/can_sender.py \
  --config pi_can_lab/sender_hazard_status.yaml \
  --experiment-id "$EXPERIMENT_ID" --execute
```

`sender_hazard_mutation.yaml`의 phased campaign은 passive baseline 10초, 정상 0x366
60초, mutation 0x366 60초, 원본 1회 복원, passive recovery 60초 순으로
진행합니다. 수신기는 campaign 전체와 마지막 recovery까지 계속 실행하십시오.
분석기는 phase tag가 있으면 normal 송신을 stimulus에서 제외하고 mutation만
비교합니다.

독립된 Raspberry Pi 사이에서 mutation 피드백은 다음 회차에 적용합니다. 첫 회차의 B-CAN
TX와 I/P-CAN RX 로그를 한 PC에서 분석한 뒤 생성된 JSON을 B-CAN 송신 장비로 복사합니다.
같은 회차 중에 수신 장비가 송신 장비를 제어하는 구조는 사용하지 않습니다.

## TX 상관분석 보고서

각 장비의 해당 회차 파일을 한 PC로 모은 다음 실행합니다. `--output`을 생략하면 분석할
때마다 `fuzz_response_1.json` + `fuzz_response_1.md`, 다음은 `_2`처럼 생성됩니다.

```bash
python3 pi_can_lab/analyze_fuzz_response.py \
  --tx pi_can_lab/logs/b_can_tx_1.jsonl \
  --rx b_can=pi_can_lab/logs/b_can_1.jsonl \
  --rx i_can=pi_can_lab/logs/i_can_1.jsonl \
  --rx p_can=pi_can_lab/logs/p_can_1.jsonl \
  --dbc A5.dbc
```

보고서의 최종 판정은 다음 의미입니다.

| 판정 | 의미 |
|---|---|
| 특이사항 없음 | 주입 직접 관측과 별도 반응 후보가 없고 분석 구간도 정상 |
| 주입 관측 / 기능 반응 미확인 | 송신 payload는 보였지만 다른 ID/신호 반응의 근거가 부족 |
| 반응 후보 관측 | baseline에서 안정적이던 비트/DBC 신호 또는 주기가 TX 직후 변화 |
| 버스 이상 관측 | 분석 구간에 overflow, error-passive, bus-off 등 CAN 오류 발생 |
| 데이터 불충분 | baseline/recovery 구간 또는 수신 frame이 부족해 판정 불가 |

단순히 baseline에 없던 payload라는 이유만으로 반응 후보로 올리지 않습니다. rolling counter,
CRC, 정상 센서 변동으로 인한 신규 payload는 안정 비트와 DBC 신호 기준으로 걸러 냅니다.
낮은 신뢰도 후보는 보고서에 참고용으로 남지만 최종 `반응 후보 관측` 개수에는 포함하지
않습니다.

## 이상반응 연계 mutation

분석 JSON의 `mutation_anomaly_mappings`는 각 반응 후보에 가장 가까운 선행 TX를 연결하고,
`anomaly_type`과 `source_mutation`에 sequence, payload, mutation 연산, 지연 시간을
기록합니다. B-CAN 송신 장비의 `pi_can_lab` 디렉터리에서 다음 회차를 preview합니다.

```bash
./lab tx --feedback logs/fuzz_response_1.json
```

preview에 표시된 seed로 실제 회차를 재현합니다.

```bash
./lab tx --feedback logs/fuzz_response_1.json \
  --random-seed SEED --execute
```

기본 random mutation은 bit 단위 연산 75%, byte 단위 연산 25%로 선택합니다. feedback이
있으면 corpus의 최대 50%를 다음 guided 전략으로 만들고 나머지는 random 탐색으로 둡니다.

| 이상반응 유형 | 다음 mutation 전략 |
|---|---|
| timing | 관련 byte의 인접값과 경계값 탐색 |
| new message | 변경 bit/byte를 되돌려 최소 trigger 탐색 |
| message disappearance | 원 mutation 재현 및 bit/byte 복원 |
| payload/signal | 변경 field의 byte/bit 국소화 |
| cross-bus | 원 mutation 재현 후보 우선 포함 |

이 매핑은 10ms 간격의 연속 TX에서 가장 가까운 선행 mutation을 연결한 시간적 후보입니다.
여러 mutation의 누적 효과나 차량 내부 지연이 있을 수 있으므로 인과관계로 확정하지 마십시오.
guided 후보는 원인을 증명하는 결과가 아니라 다음 재현 실험의 입력입니다.

## 유의미한 퍼징 결과의 조건

한 번의 시간 상관만으로 인과관계를 확정하지 마십시오.

1. 같은 seed와 입력을 3회 이상 반복해 같은 ID/신호가 같은 지연으로 변하는지 확인합니다.
2. payload를 바꾸지 않는 no-op 대조 회차에서도 후보가 나오는지 비교합니다.
3. TX ID의 직접 관측은 전달 증거일 뿐 기능 반응에서 분리합니다.
4. recovery에서 정상 안정 비트/신호로 돌아오는지 확인합니다.
5. CAN 오류가 있는 회차는 기능 결과와 버스 과부하 결과를 분리합니다.
6. 실제 램프, 모터, 릴레이 동작은 영상·전류·GPIO 등 별도 physical oracle로 기록합니다.
7. 매 회차의 자동 생성 seed를 TX manifest에서 보관하고, 유의미한 회차는
   `--random-seed`로 재현하여 동일 반응이 나오는지 확인합니다.

JSONL은 삭제하거나 요약본으로 대체하지 마십시오. 판정 기준이 바뀌어도 원본으로 다시
분석할 수 있어야 합니다.
