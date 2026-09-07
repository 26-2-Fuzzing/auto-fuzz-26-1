# Iteration-based Feedback 실험 운영

이 파이프라인은 현재 Trial의 partial log를 사용하지 않습니다. 항상 Trial N의 송신과 세 버스
캡처가 완전히 종료되고 SFTP 다운로드와 분석이 끝난 뒤 `feedback_state.json`을 갱신합니다.
Trial N+1의 mutation selector만 그 갱신된 상태를 읽습니다.

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
  → baseline/mutation 통계
  → anomaly + mutation mapping
  → feedback.json + feedback_state.json
  → Trial N+1 selector
```

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

격리된 벤치에서 확인 후 실제 실행합니다.

```bash
python3 experiment_runner.py \
  --target-id 0x366 \
  --source-bus B_CAN \
  --trials 20 \
  --random-seed 366 \
  --execute
```

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
│   └── sender.stderr.log
└── trial_0002/
```

`experiments/`와 실제 SSH 설정은 `.gitignore` 대상입니다. GitHub는 소스 배포에만 사용하고
실험 로그 전달에는 사용하지 않습니다. Control PC의 raw JSONL은 자동 삭제하지 않습니다.
원격 Pi에도 `/tmp/auto_fuzz_trials` 아래 원본이 남으므로 용량 정리는 실험자가 확인 후 별도로
수행합니다.

## FeedbackState

`feedback_state.json`은 완료된 Trial만 반영하며 다음 정보를 누적합니다.

- `total_trials`, `next_mutation_id`
- 모든 `mutation_history`
- 점수순 `interesting_mutations`와 anomaly type
- operator별 `executed`/`interesting` 통계
- `last_feedback`

Mutation에는 원본/변경 payload, operator, byte/bit, DBC decode가 가능한 signal 변화,
`parent_mutation_id`, `generation_reason`, `strategy_mode`가 기록됩니다.
`reproduction_of_mutation_id`로 동일 mutation 반복 회차를 묶을 수 있습니다. 자동 재현 정책은
없지만 `--reproduce-mutation-id`로 명시적 반복 실행할 수 있습니다.

## 전략과 재현성

- 첫 Trial: 기존 로컬 `mutation_engine.py`로 exploration
- 직전 완료 Trial이 흥미롭지 않음: 기본 80% exploration, 20% revisit
- 흥미로운 anomaly 발견: 기본 70% parent 주변 exploitation, 30% exploration
- timing/frequency: 값 주변과 boundary 우선
- payload/new/loss/cross-bus: 같은 byte의 다른 bit와 인접 값 우선

모든 비율과 anomaly threshold는 `experiment_runner.yaml`에서 바꿉니다. 동일한
`random_seed + feedback_state + config + baseline payload`는 동일 선택을 재현합니다. 실제
baseline payload가 달라지면 안전하고 의미 있는 mutation을 위해 결과도 달라질 수 있습니다.

## 현재 한계

- 자동 reproduction policy는 아직 없고 schema/interface만 준비돼 있습니다.
- 물리 램프·모터 반응은 CAN 로그만으로 판정하지 않습니다.
- DBC signal metadata는 A5.dbc에서 원본과 mutation payload 모두 decode될 때만 기록됩니다.
- clock offset은 경고와 기록만 하며 RX timestamp를 자동 보정하거나 시스템 시각을 변경하지 않습니다.
- SSH 중단·네트워크 장애가 발생한 Trial은 `failed`로 남고 FeedbackState에는 반영되지 않습니다.
