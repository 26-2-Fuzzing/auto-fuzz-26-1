# pi_can_lab Fuzzer 실행 가이드

## 1. 실행 구조

변경된 Fuzzer는 노트북 한 대를 Control PC로 사용하고, 같은 핫스팟에 연결된 세 Raspberry Pi를 SSH/SFTP로 제어한다.

```text
노트북(Control PC)
├─ SSH/SFTP → P-CAN Pi: P-CAN 수신
├─ SSH/SFTP → B-CAN Pi: B-CAN 수신 + 0x366 Injection
└─ SSH/SFTP → I-CAN Pi: I-CAN 수신
```

`experiment_runner.py`는 노트북의 로컬 터미널에서 한 번만 실행한다. 각 Pi에서 `./lab rx`나 `./lab tx`를 별도로 실행하지 않는다.

한 Trial이 완전히 끝나고 세 로그가 회수·분석된 뒤에 관측 후보를 기록한다. 현재 stage 1에서는
자동 Feedback 선택을 차단하고, 다음 Trial은 일반 exploration으로 진행한다. 실행 중인 Trial의
Mutation은 변경하지 않는다.

## 2. 사전 준비

### 세 Raspberry Pi 공통

세 Pi에는 같은 버전의 `pi_can_lab` 코드와 `A5.dbc`가 있어야 한다. 각 Pi의 `pi_can_lab` 디렉터리에서 최초 한 번 실행한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
sudo systemctl enable --now ssh
```

CAN 인터페이스를 활성화한다. `YOUR_BITRATE`는 해당 CAN Bus의 실제 bitrate로 바꿔야 하며 추측한 값을 사용하지 않는다.

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate YOUR_BITRATE
sudo ip link set can0 up
ip -details -statistics link show can0
```

각 Pi의 핫스팟 IP를 확인한다.

```bash
hostname -I
```

같은 핫스팟에 연결돼도 장치별 IP는 서로 달라야 한다. 예를 들면 다음과 같다.

```text
Control PC : 192.168.137.1
P-CAN Pi   : 192.168.137.21
B-CAN Pi   : 192.168.137.22
I-CAN Pi   : 192.168.137.23
```

### Control PC

노트북의 `pi_can_lab` 디렉터리에서 환경을 준비한다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp experiment_runner.yaml.example experiment_runner.yaml
```

현재 예시는 30/10/1/20초 수집, 50 ms 송신, 자동 feedback 비활성화인 stage 1 설정이다.
이 구간의 anomaly는 검증된 차량 반응이 아니다. no-op 대조 및 held-out 세션 검증 없이
`experiment_0001`만으로 현장 false-positive rate를 주장하지 않는다. 자세한 절차는
[`CALIBRATION.md`](CALIBRATION.md)를 참고한다.

Windows의 WSL에서 실행한다면 이후 네트워크 확인과 Fuzzer 실행도 동일한 WSL 터미널에서 수행한다.

## 3. SSH 설정

Control PC의 공개키를 세 Pi의 `~/.ssh/authorized_keys`에 등록한다. 비밀번호를 YAML에 직접 기록하지 않는다.

Control PC에서 다음 세 접속이 모두 성공해야 한다.

```bash
ssh pi@P_CAN_PI_IP
ssh pi@B_CAN_PI_IP
ssh pi@I_CAN_PI_IP
```

최초 접속 때 host key를 확인해 `known_hosts`에 등록하고, 암호 없이 키로 접속되는지 확인한다.

## 4. experiment_runner.yaml 설정

노트북에 생성한 `experiment_runner.yaml`의 실제 IP, 사용자, SSH key, 각 Pi의 프로젝트 경로를 입력한다.

```yaml
experiments_root: experiments
dbc: ../A5.dbc
sender_config: sender_trial.yaml

remote:
  project_dir: /home/pi/auto-fuzz-26/pi_can_lab
  python: /home/pi/auto-fuzz-26/pi_can_lab/.venv/bin/python
  capture_root: /tmp/auto_fuzz_trials

  hosts:
    p_can:
      host: 192.168.137.21
      user: pi
      key_filename: /home/CONTROL_USER/.ssh/id_ed25519

    b_can:
      host: 192.168.137.22
      user: pi
      key_filename: /home/CONTROL_USER/.ssh/id_ed25519

    i_can:
      host: 192.168.137.23
      user: pi
      key_filename: /home/CONTROL_USER/.ssh/id_ed25519
```

예시 IP와 경로는 실제 환경에 맞게 반드시 변경한다. Pi마다 프로젝트 경로나 Python 경로가 다르면 각 host 항목에 `project_dir` 또는 `python`을 별도로 지정할 수 있다.

## 5. 실행 전 점검

각 Pi에서 다음을 확인한다.

```bash
ip -details -statistics link show can0
```

B-CAN Pi에서는 정상 `0x366` 프레임이 실제로 수신되는지 확인한 뒤 `Ctrl+C`로 종료한다.

```bash
candump can0,366:7FF
```

실험 전에 세 장비의 NTP/chrony 동기화 상태도 확인한다. Runner는 시각 offset을 기록하고 임계값 초과 시 경고하지만 시스템 시각을 자동 변경하지 않는다.

```bash
chronyc tracking
```

## 6. Fuzzing 실행

다음 명령은 모두 **Control PC의 `pi_can_lab` 디렉터리**에서 실행한다.

### Preview

Preview는 SSH 연결이나 CAN 송신을 시작하지 않고 설정만 확인한다.

```bash
source .venv/bin/activate

python3 experiment_runner.py \
  --target-id 0x366 \
  --source-bus B_CAN \
  --paired-cycle \
  --cycle-max-sets 10 \
  --random-seed 366
```

### 실제 실행

허가된 시험 차량 또는 격리된 벤치에서만 `--execute`를 추가한다.

```bash
python3 experiment_runner.py \
  --target-id 0x366 \
  --source-bus B_CAN \
  --paired-cycle \
  --cycle-max-sets 10 \
  --random-seed 366 \
  --execute
```

각 옵션의 의미는 다음과 같다.

- `--target-id 0x366`: Mutation 대상 CAN ID
- `--source-bus B_CAN`: Injection을 수행할 Pi와 CAN Bus
- `--paired-cycle`: 0x366의 실제 변조 계열 8개를 정해진 순서로 순회한다. 동일한
  송신 내용과 안전 제한을 통과하지 못한 후보는 건너뛰고 사유를 계획에 기록한다.
- `--cycle-max-sets 10`: 이번 명령에서 최대 10세트만 실행한다. 다음 실행은 같은
  `--experiment-id`와 설정으로 이어간다. 기본값도 10세트다.
- `--paired-sets 10`: 순차 사이클 대신 mutation과 no-op을 한 번씩 포함한 세트 10개를
  기존 선택기로 실행한다. 두 구간의
  순서는 세트마다 교대하며, 각 구간은 별도의 baseline/normal/recovery를 수집한다.
- `--trials N`: 대조 세트가 아닌 기존 단독 Trial을 N개 실행할 때 사용한다.
- `--random-seed 366`: Mutation 선택 재현을 위한 seed
- `--execute`: 실제 SSH 접속과 CAN 송신 허용

## 7. 자동 수행되는 동작

권장 paired mode에서는 mutation을 먼저 고정하고, 한 세트에서 다음 과정을 수행한다.

```text
정상 0x366 원본 확보 및 이번 세트의 mutation 고정
→ 첫 구간: P/B/I 수신, baseline, normal 송신, mutation 또는 no-op, recovery
→ 로그 회수 및 첫 recovery 상태 복귀 확인
→ 복귀가 확인되지 않으면 둘째 구간 송신 중단
→ 원본 payload 재확인 후 둘째 구간 전체를 새로 캡처
→ 두 구간의 사전 상태·TX 패턴·RX/시계 품질·이상 후보를 짝 비교
→ 비교 가능하면 feedback·exploit 없이 다음 세트로 진행
→ 비교 보류면 사유를 저장하고 캠페인 중단
```

한 구간은 예시 설정에서 61초이므로 세트당 송신·캡처만 최소 122초다. 회복과
원본 일치가 확인되지 않으면 다음 구간을 보내지 않는다. 한 세트에서 mutation 쪽에만
이상 후보가 나와도 검증 완료가 아니다. 별도 상태 일치 반복과 물리적 반응 확인이
필요하며, 자동 exploitation은 비활성화돼 있다.

순차 사이클은 **한 세트에 변조 후보 하나**만 적용한 뒤 다음 후보로 넘어간다.
계열 순서는 `signal_single → signal_combination → state_contradiction →
undefined_enum → undefined_bit_single → undefined_bit_multi →
defined_undefined_mix → temporal_sequence`다. `all-0x366`은 이 계열들의
합집합이므로 별도 다섯 번째 단계로 반복하지 않는다. 기본 DBC·기준 payload·1초
변조 설정에서는 원시 후보 361개 중 281개를 실행 계획에 넣고 80개는 중복 송신
또는 시간·안전 제한으로 제외한다. `undefined_enum`은 앞선 계열과 송신 내용이
전부 같아 실행 0개로 기록된다. 실제 live 원본 payload나 설정이 다르면 수가
달라진다. 전체 281세트는 캡처·송신 구간만 최소 9시간 31분 22초이므로 한 번에
무제한 실행하지 말고 상태와 품질을 확인하면서 나누어 진행한다. 이 순회는 후보
탐색이며 anomaly의 재현성 검증은 별도의 반복 실험으로 수행한다.

## 8. 기존 Experiment 재개와 Mutation 재현

순차 사이클이 10세트 상한에서 멈춘 경우, 출력된 Experiment ID(예: 42)를
사용해 다음 묶음을 이어서 실행한다. 완료된 세트는 다시 송신하지 않는다.

```bash
python3 experiment_runner.py \
  --experiment-id 42 --target-id 0x366 --source-bus B_CAN \
  --paired-cycle --cycle-max-sets 10 --random-seed 366 --execute
```

별도로 생성한 단독 Trial Experiment 43을 이어서 실행하려면 다음처럼 한다. 사이클과
단독 Trial은 한 Experiment ID에 섞을 수 없다.

```bash
python3 experiment_runner.py \
  --experiment-id 43 \
  --target-id 0x366 \
  --source-bus B_CAN \
  --trials 5 \
  --random-seed 366 \
  --execute
```

완료된 Mutation 84를 네 번 반복 재현한다.

```bash
python3 experiment_runner.py \
  --experiment-id 43 \
  --target-id 0x366 \
  --source-bus B_CAN \
  --trials 4 \
  --reproduce-mutation-id 84 \
  --random-seed 366 \
  --execute
```

원 Trial과 현재 live baseline payload가 다르면 안전을 위해 재현 실행이 중단된다.

## 9. 결과 확인

Control PC에 결과가 다음 구조로 저장된다.

```text
experiments/experiment_0042/
├── experiment.json
├── feedback_state.json
├── pairs/
│   ├── cycle.json
│   ├── pair_0001.json
│   └── pair_0001_report.json
└── trial_0001/
    ├── metadata.json
    ├── mutation.json
    ├── tx.jsonl
    ├── p_can.jsonl
    ├── b_can.jsonl
    ├── i_can.jsonl
    ├── anomalies.json
    ├── feedback.json
    └── sender.stdout.log
```

원격 Pi에도 `/tmp/auto_fuzz_trials` 아래 raw log가 남는다. Control PC와 Pi의 raw log는 Runner가 자동 삭제하지 않는다. 완료되지 않은 Trial은 FeedbackState에 반영되지 않는다.
연결이 갑자기 끊기면 metadata가 `running`에 남을 수 있으므로 TX 완료 마커와 수신 로그를 확인한다.

## 10. 주의사항

- 자동 Runner 실행 중 각 Pi에서 `./lab rx`, `./lab tx`, 별도 Injection 프로세스를 실행하지 않는다.
- 세 Pi의 IP가 변경되면 `experiment_runner.yaml`을 갱신한다.
- 핫스팟에서 클라이언트 격리가 활성화돼 SSH가 차단되지 않았는지 확인한다.
- `0x366` 정상 프레임과 CAN bitrate가 확인되지 않으면 실제 Injection을 시작하지 않는다.
- 실제 주행 중에는 송신하지 않고, 허가된 차량이나 격리된 시험 환경에서만 실행한다.
- 현재 Trial의 partial log는 같은 Trial의 Mutation 선택에 사용되지 않는다.

## 11. 0x366 전용 Mutation profile

`--mutation-profile`을 생략하면 기존 generic bit/byte/random mutation을 그대로 사용한다. 신규 DBC-aware campaign은 다음 중 하나를 명시한다.

```text
signal-aware
undefined-only
semantic-plus-undefined
temporal
all-0x366
```

예를 들어 DBC 미정의 bit만 시험하려면 다음과 같이 실행한다.

```bash
python3 experiment_runner.py \
  --target-id 0x366 \
  --source-bus B_CAN \
  --mutation-profile undefined-only \
  --undefined-max-bits 2 \
  --trials 20 \
  --random-seed 366 \
  --execute
```

전체 occupancy와 undefined enum을 송신 없이 확인한다.

```bash
python3 experiment_runner.py \
  --config experiment_runner.yaml \
  --print-0x366-map
```

상세 family, bit map, enum 및 payload sample은 `A5_0X366_MUTATIONS.md`를 참고한다.
