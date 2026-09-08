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

한 Trial이 완전히 끝나고 세 로그가 회수·분석된 뒤에만 그 Feedback을 다음 Trial Mutation 생성에 사용한다. 실행 중인 Trial의 Mutation을 실시간으로 변경하지 않는다.

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
  --trials 20 \
  --random-seed 366
```

### 실제 실행

허가된 시험 차량 또는 격리된 벤치에서만 `--execute`를 추가한다.

```bash
python3 experiment_runner.py \
  --target-id 0x366 \
  --source-bus B_CAN \
  --trials 20 \
  --random-seed 366 \
  --execute
```

각 옵션의 의미는 다음과 같다.

- `--target-id 0x366`: Mutation 대상 CAN ID
- `--source-bus B_CAN`: Injection을 수행할 Pi와 CAN Bus
- `--trials 20`: 완료할 Trial 수
- `--random-seed 366`: Mutation 선택 재현을 위한 seed
- `--execute`: 실제 SSH 접속과 CAN 송신 허용

## 7. 자동 수행되는 동작

Runner는 각 Trial마다 다음 과정을 자동으로 수행한다.

```text
P/B/I-CAN 수신 동시 시작
→ 세 수신 프로세스 생존 확인
→ B-CAN의 정상 0x366 payload 확보
→ Baseline 관찰
→ 정상 0x366 송신
→ 단일 Mutation 반복 송신
→ Post-Mutation 관찰
→ 세 수신 종료
→ SFTP 로그 회수
→ Baseline vs Mutation 분석
→ Anomaly 및 Feedback 저장
→ 다음 Trial Mutation 선택
```

Trial 1은 기존 Mutation Engine으로 시작한다. Trial 2부터 완료된 이전 Trial의 `feedback_state.json`을 읽어 Exploration 또는 Exploitation 전략을 선택한다.

## 8. 기존 Experiment 재개와 Mutation 재현

기존 Experiment 42를 이어서 실행한다.

```bash
python3 experiment_runner.py \
  --experiment-id 42 \
  --target-id 0x366 \
  --source-bus B_CAN \
  --trials 5 \
  --random-seed 366 \
  --execute
```

완료된 Mutation 84를 네 번 반복 재현한다.

```bash
python3 experiment_runner.py \
  --experiment-id 42 \
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
└── trial_0001/
    ├── metadata.json
    ├── mutation.json
    ├── tx.jsonl
    ├── p_can.jsonl
    ├── b_can.jsonl
    ├── i_can.jsonl
    ├── anomalies.json
    ├── feedback.json
    ├── sender.stdout.log
    └── sender.stderr.log
```

원격 Pi에도 `/tmp/auto_fuzz_trials` 아래 raw log가 남는다. Control PC와 Pi의 raw log는 Runner가 자동 삭제하지 않는다. 네트워크나 캡처 실패로 완료되지 않은 Trial은 `failed`로 남고 다음 Mutation의 FeedbackState에는 반영되지 않는다.

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
