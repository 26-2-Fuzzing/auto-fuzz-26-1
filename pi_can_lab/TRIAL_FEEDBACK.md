# Iteration-based Feedback 실험 운영

현재 stage 1 기본 설정은 `feedback.enabled: false`입니다. 자동 feedback-guided mutation과
exploitation은 비활성화하고 anomaly 판정의 오탐을 먼저 교정합니다. 각 Trial의 송신과 세 버스
캡처가 완전히 끝난 뒤에만 분석 결과를 기록합니다. `candidate`는 관측된 단일 Trial 변화이지
검증된 반응이나 다음 mutation의 parent가 아닙니다. 오프라인 검증 절차는
[`CALIBRATION.md`](CALIBRATION.md)를 참고하십시오.

## 구조

```text
Control PC
  experiment_runner.py
       ├─ SSH: P-CAN Pi can_receiver.py
       ├─ SSH: B-CAN Pi can_receiver.py / 선택된 source에서 can_sender.py
       └─ SSH: I-CAN Pi can_receiver.py

Trial N: 단일 mutation 반복 송신
  → 캡처 종료
  → SFTP로 모든 JSONL 회수
  → 이전 완료 Trial과 현재 baseline/normal/mutation 대조
  → anomaly 후보/보류 분류 + mutation mapping (인과 검증 아님)
  → feedback.json + feedback_state.json
  → Trial N+1은 feedback 비활성화 상태에서 일반 exploration
```

권장 실행 단위는 **mutation/no-op 짝 세트**입니다. 같은 원본 payload와 미리 고정한
mutation을 사용해 각각 `baseline → normal → mutation 또는 no-op → recovery` 전체를
새로 캡처합니다. 두 구간의 순서는 세트마다 교대하여 순서 효과를 줄입니다. 새
`--paired-cycle`에서는 첫 구간의 원본 payload나 캡처 무결성을 확인할 수 없으면 둘째
구간 송신을 중단합니다. 조명·잠금 상태 변화는 기록하며 자동 중단 조건으로 삼지
않습니다. 짝 비교는 수집 후 진행하고 결과는 `diagnostic / unverified`이며 자동
exploit이나 다음 mutation 선택에 사용되지 않습니다. 기존 `--paired-sets`의 즉시
분석 방식은 비교 결과가 `inconclusive`이면 후속 세트를 중단할 수 있습니다.

## 최초 설정

세 Raspberry Pi 모두 동일한 소스 버전을 배포하고 SocketCAN을 먼저 올립니다.

```bash
sudo ip link set can0 up type can bitrate YOUR_BITRATE
ip -details link show can0
candump can0
```

Control PC의 `pi_can_lab`에서 환경을 만들고 SSH 라이브러리를 설치합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp experiment_runner.yaml.example experiment_runner.yaml
```

`experiment_runner.yaml`의 IP, 사용자, 각 Pi의 `project_dir`, Control PC의 SSH key 경로를
수정합니다. 공개키를 각 Pi의 `~/.ssh/authorized_keys`에 설치하고, 먼저 직접 접속해 host key를
`known_hosts`에 등록하십시오. 비밀번호를 YAML에 저장하지 않습니다. 꼭 필요한 경우
`password_env`에 환경변수 이름만 적습니다.

모든 Pi에는 `python3`, `candump`(can-utils), `can_receiver.py`, `can_sender.py`,
`sender_trial.yaml` 및 각 receiver YAML이
있어야 합니다. `chronyc tracking`은 선택 사항이며 runner는 시간을 변경하지 않고 offset과
chrony 결과만 metadata에 기록합니다.

## 실행

설정만 확인하는 안전 preview는 SSH나 CAN에 접근하지 않습니다.

```bash
python3 experiment_runner.py --target-id 0x366 --source-bus B_CAN --trials 20
```

격리된 벤치에서 확인 후 단독 Trial을 실행하는 기존 방식은 다음과 같습니다.

```bash
python3 experiment_runner.py \
  --target-id 0x366 \
  --source-bus B_CAN \
  --mutation-profile all-0x366 \
  --undefined-max-bits 2 \
  --trials 20 \
  --random-seed 366 \
  --execute
```

현재 권장하는 순차 탐색은 `--paired-cycle`입니다. 한 변조 후보에 mutation/no-op
짝 세트 하나를 적용하고, 8개 실제 계열을 순서대로 소진합니다. `--paired-sets 10`은
기존 선택기로 독립 짝 세트 10개를 실행하는 별도 방식이며, `--trials`는 단독 Trial용입니다.
먼저 `--execute` 없이 preview한 다음 실제 실행합니다.

```bash
python3 experiment_runner.py \
  --target-id 0x366 --source-bus B_CAN \
  --paired-cycle --cycle-max-sets 10 \
  --undefined-max-bits 2 --random-seed 366 --execute
```

기본 상한은 명령 한 번당 10세트입니다. 멈춘 지점부터 이어가려면 같은 설정과
`--experiment-id`를 지정해 다시 실행합니다. 이 상한에서 중단된 경우 Experiment는
완료로 표시하지 않습니다. 기존 Trial이나 일반 짝 세트와 같은 Experiment ID를
혼합하지 않습니다. 후보는 실제 송신 프레임열을 기준으로 중복 제거하고 50 ms/20프레임·
완전한 temporal 패턴 조건을 만족하지 않으면 스킵 사유를 남깁니다. 기본
`A5.dbc`·기준 payload에서는 원시 361개 중 281개가 실행 계획에 포함됩니다.
`undefined_enum` 53개는 앞선 `signal_single`과 송신 내용이 모두 같아 별도
송신하지 않습니다. `all-0x366`은 계열의 합집합이지 추가 단계가 아닙니다.

새 순차 사이클은 실행 중 원본 payload와 TX/RX 캡처 무결성을 확인하고,
조명·잠금 등 복구 상태 변화는 관측으로 기록합니다. 상세 anomaly/feedback·페어
비교는 수집이 끝난 뒤 오프라인 명령으로 작성합니다. 상태 변화 자체는 다음 송신을
자동 중단하지 않으며, 0x3D6의 `LH_Aussenlicht_def` 신호 변화만
`review_required`에서 제외합니다. 캡처 무결성이나 원본 payload 확인에 실패하면
다음 송신을 중단하고 이미 수집한 로그는 보존합니다.

```bash
python3 experiment_runner.py \
  --config experiment_runner.yaml \
  --experiment-id 42 \
  --finalize-analysis
```

`--finalize-analysis`는 SSH 연결과 CAN 송신 없이 저장된 로그를 분석하며, 중단 후
재실행하면 완료되지 않은 분석부터 이어갑니다. 기존에 생성된 순차 사이클은
기존 실행 방식을 유지합니다.

각 구간의 예시 수집 시간은 30+10+1+20=61초이므로 한 세트는 송신·캡처 구간만
최소 122초이며, 장비 시작/정지·회복 확인·원본 재측정 시간이 추가됩니다. 시간만
늘려 표본을 확보하는 대신 상태가 맞는 독립 세트를 반복하고, 부족한 저빈도 ID는
판정 보류합니다. 실제 장비에서 안전한 원본 송신 및 회복이 확인된 격리 벤치에서만
`--execute`를 사용하십시오.

이 순차 탐색은 서로 다른 후보를 한 번씩 관찰하는 단계입니다. 후보가 나와도
검증된 anomaly로 간주하지 않으며, 이후 같은 후보의 상태 일치 독립 세트를
반복하고 no-op 오탐률과 물리적 반응을 따로 확인해야 합니다.

`--mutation-profile`을 생략하면 기존 generic mutation engine을 그대로 사용합니다. 전용 profile은
`signal-aware`, `undefined-only`, `semantic-plus-undefined`, `temporal`, `all-0x366`이며 상세
occupancy/enum/case 정의는 `A5_0X366_MUTATIONS.md`를 참고합니다.

`--source-bus P_CAN` 또는 `I_CAN`도 지원하지만, 실제 배선과 주입 권한이 있는 Pi인지 먼저
확인해야 합니다. 기존 experiment를 이어가려면 `--experiment-id 42`를 추가합니다. 실패한
Trial 디렉터리는 덮어쓰지 않으며 다음 번호로 재개됩니다.

완료된 mutation 84를 네 번 명시적으로 재현하려면 다음처럼 실행합니다. live baseline이
원 Trial의 baseline과 다르면 안전을 위해 중단합니다.

```bash
python3 experiment_runner.py --experiment-id 42 \
  --target-id 0x366 --source-bus B_CAN --trials 4 \
  --reproduce-mutation-id 84 --random-seed 366 --execute
```

## 저장 구조

```text
experiments/experiment_0042/
├── experiment.json
├── feedback_state.json
├── trial_0001/
│   ├── metadata.json
│   ├── mutation.json
│   ├── tx.jsonl
│   ├── p_can.jsonl
│   ├── b_can.jsonl
│   ├── i_can.jsonl
│   ├── anomalies.json
│   ├── feedback.json
│   ├── sender.stdout.log
│   └── ...
├── trial_0002/
└── pairs/
    ├── pair_0001.json         # 고정된 세트 계획·진행 상태
    └── pair_0001_report.json  # 완료 후 대조 보고서
```

`experiments/`와 실제 SSH 설정은 `.gitignore` 대상입니다. GitHub는 소스 배포에만 사용하고
실험 로그 전달에는 사용하지 않습니다. Control PC의 raw JSONL은 자동 삭제하지 않습니다.
원격 Pi에도 `/tmp/auto_fuzz_trials` 아래 원본이 남으므로 용량 정리는 실험자가 확인 후 별도로
수행합니다.

## FeedbackState

`feedback_state.json`은 완료된 Trial만 반영하며 다음 정보를 누적합니다.

- `total_trials`, `next_mutation_id`, 멱등 완료 처리를 위한 `completed_trial_ids`
- no-op만 집계하는 `total_control_trials`, `control_trial_ids`
- 모든 `mutation_history`
- 과거 완료 Trial의 Baseline/Normal에서 관측한 payload와 현재 Trial의 동일 길이 변조 전 대조 창
- 재검증 전 `feedback_candidates`; 구 schema의 `interesting_mutations`는 선택에 사용하지 않음
- operator별 `executed`/`interesting` 통계
- `last_feedback`

Mutation에는 원본/변경 payload, operator, byte/bit, DBC decode가 가능한 signal 변화,
`parent_mutation_id`, `generation_reason`, `strategy_mode`가 기록됩니다.
`reproduction_of_mutation_id`로 동일 mutation 반복 회차를 묶을 수 있습니다. 자동 재현 정책은
없지만 `--reproduce-mutation-id`로 명시적 반복 실행할 수 있습니다.

`PAYLOAD_CHANGE`는 새로운 전체 payload라는 이유만으로 확정하지 않습니다. 같은 길이의
직전 normal 창, DBC 신호/동적 필드, 과거 완료 Trial, recovery 지속성 및 시계 정렬의 불확실성을
함께 기록합니다. `feedback.json`은 candidate와 no-op control 관측을 분리하지만 현재 자동
`verified` 승격은 하지 않고 `feedback_eligible`도 `false`입니다. 서로 다른 Trial에서 같은
payload가 반복되더라도 차량 반응의 인과 검증이 아닙니다. 기존 schema의 높은 점수나
`interesting_mutations`도 자동 exploitation 근거로 사용하지 않습니다.

## 전략과 재현성

- `feedback.enabled: false`: 매 Trial 일반 exploration; 후보 점수와 기존 feedback state를
  parent로 사용하지 않음
- no-op control: mutation slot에서도 원본 payload를 같은 50 ms 간격으로 송신하고 별도
  control 결과로 기록; `tx.jsonl`의 실제 송신 건수·payload·rate를 검증해야 비교 가능
- paired set: mutation을 첫 구간 전에 한 번만 선택하고 원본·변조 payload를 고정한다.
  둘째 구간 직전 live 원본이 달라지거나 회복/캡처/시간 정렬/송신 패턴 증거가 부족하면
  짝 비교를 보류한다. temporal mutation이면 no-op도 동일한 mutation 구간 간격으로
  원본을 송신한다. 미완료 세트를 자동 재전송·재사용하지 않는다.
- 모든 anomaly 유형: 현재는 관측/보류 정보일 뿐 자동 exploitation에 사용하지 않음

anomaly threshold와 송신 상한은 `experiment_runner.yaml`/`sender_trial.yaml`에서 확인합니다.
현재 stage 1 예시는 baseline/normal/mutation/recovery 30/10/1/20초, 50 ms 간격이며
1초 mutation 구간의 저빈도 ID는 검증 불가로 보류할 수 있습니다. 동일한
`random_seed + feedback_state + config + baseline payload`는 동일 선택을 재현합니다. 실제
baseline payload가 달라지면 안전하고 의미 있는 mutation을 위해 결과도 달라질 수 있습니다.
주기가 완전히 일정한 baseline에서 새 jitter가 발생하는 경우에는
`timing_stddev_absolute_ms` 절대 임계값을 사용합니다.
기존 설정에 `no_anomaly.exploration_probability`가 남아 있어도 무시하며, 미검증 mutation을
재방문하던 20% 분기는 현재 사용하지 않습니다. 기존 experiment의 schema 1
`interesting_mutations`도 자동으로 검증된 것으로 간주하지 않습니다.

분석 산출물 기록 후 상태는 `analyzed`를 거쳐 `completed`가 됩니다. 이 사이에 runner가
중단되면 다음 실행이 Trial ID 기준으로 FeedbackState를 중복 없이 반영하고 완료 상태를
복구합니다.

## 현재 한계

- 자동 reproduction policy는 아직 없고 schema/interface만 준비돼 있습니다.
- 물리 램프·모터 반응은 CAN 로그만으로 판정하지 않습니다.
- DBC signal metadata는 A5.dbc에서 원본과 mutation payload 모두 decode될 때만 기록됩니다.
- 분석기는 source host 대비 상대 clock offset을 적용하고 불확실성을 기록하지만 시스템 시각은 변경하지 않습니다.
- 기존 `experiment_0001`은 no-op 검증 세션이나 현장 오탐률을 추정할 만큼 독립적인 음성 세션이 아닙니다.
- 정상 완료가 확인되지 않은 Trial은 FeedbackState에 반영되지 않습니다. 연결이 끊겨 metadata가
  `running`으로 남을 수 있으므로 TX 완료 마커와 수신 로그를 먼저 확인해야 합니다.
