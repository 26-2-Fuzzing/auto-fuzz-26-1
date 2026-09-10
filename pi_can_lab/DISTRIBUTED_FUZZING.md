# 분리된 3개 노트북/Pi 환경 실행 가이드

## 구조

네트워크 문제로 한 노트북이 세 Raspberry Pi에 동시에 접속하지 못할 때 사용한다.

```text
B-CAN 노트북(Control PC) ─ SSH → B-CAN Pi: mutation 생성 및 injection/TX 저장
P-CAN 노트북             ─ SSH → P-CAN Pi: RX 저장만 수행
I-CAN 노트북             ─ SSH → I-CAN Pi: RX 저장만 수행
```

B-CAN RX는 수집하지 않는다. 한 Trial의 분석 입력은 `B-CAN TX + P-CAN RX + I-CAN RX`이다.
현재 Trial 실행 중 mutation을 바꾸지 않으며, 두 RX 결과와 TX 결과가 Control PC에 모두
모인 뒤에만 feedback을 확정한다. 따라서 실시간 feedback은 아니지만 기존의
`Trial N 결과 → Trial N+1 mutation` 방식은 그대로 사용할 수 있다.

현재 장비 연결 정보:

| 역할 | SSH 사용자 | Raspberry Pi IP | 수행 작업 |
|---|---|---|---|
| B-CAN Control PC | `kuse0810` | `172.20.10.14` | Mutation 준비, injection, TX 저장, 최종 분석 |
| P-CAN 노트북 | `myung` | `172.20.10.13` | P-CAN RX 저장 |
| I-CAN 노트북 | `i` | `172.20.10.2` | I-CAN RX 저장 |

## 최초 설정

세 노트북과 세 Pi 모두 같은 commit의 `pi_can_lab`을 사용한다. 각 노트북에서:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

B-CAN Control PC에서는:

```bash
cp distributed_runner.yaml.example distributed_runner.yaml
cp distributed_node.yaml.example distributed_node.yaml
```

`distributed_node.yaml`의 `node.bus`를 `B_CAN`으로 바꾸고 B-CAN Pi의 SSH 정보와
프로젝트 경로를 입력한다. P/I 노트북에서도 각각 파일을 복사하고 `P_CAN`, `I_CAN` 및
자신에게 연결된 Pi의 SSH 정보를 입력한다. `allow_unknown_host_key`는 기본값 `false`를
유지하고 최초 SSH 접속에서 실제 host key를 확인한다.

각 노트북에는 서로 다른 `distributed_node.yaml`이 필요하다.

```yaml
# B-CAN Control PC
node:
  bus: B_CAN
  ssh:
    host: 172.20.10.14
    user: kuse0810
```

```yaml
# P-CAN 노트북
node:
  bus: P_CAN
  receiver_config: receiver_p_can.yaml
  ssh:
    host: 172.20.10.13
    user: myung
```

```yaml
# I-CAN 노트북
node:
  bus: I_CAN
  receiver_config: receiver_i_can.yaml
  ssh:
    host: 172.20.10.2
    user: i
```

위 예시는 핵심 필드만 표시한다. 실제 파일에는 `project_dir`, `python`, `remote_root`,
`results_dir`, SSH key 설정도 유지해야 한다.

세 Pi의 시각은 NTP/chrony로 동기화되어야 한다. 코드는 시스템 시각을 변경하지 않는다.

```bash
timedatectl status
chronyc tracking
```

## Trial 1: mutation 패키지 생성

B-CAN Control PC의 `pi_can_lab`에서 실행한다.

```bash
python3 distributed_runner.py prepare \
  --config distributed_runner.yaml \
  --experiment-id 42 \
  --target-id 0x366 \
  --random-seed 366 \
  --mutation-profile all-0x366
```

출력된 ZIP 패키지를 P-CAN 및 I-CAN 노트북에 복사한다. USB, 로컬 파일 공유 등 실험에
허용된 전달 수단을 사용한다. raw 실험 로그를 Git 저장소로 주고받지 않는다.

## P-CAN과 I-CAN 수신 시작

두 노트북에서 각자 `pi_can_lab` 디렉터리 안에서 먼저 실행한다.

P-CAN 노트북:

```bash
python3 distributed_runner.py receive \
  --config distributed_node.yaml \
  --package experiment_0042_trial_0001.zip \
  --bus P_CAN
```

I-CAN 노트북:

```bash
python3 distributed_runner.py receive \
  --config distributed_node.yaml \
  --package experiment_0042_trial_0001.zip \
  --bus I_CAN
```

두 화면 모두 `[CAPTURE] ... started`를 표시한 뒤 B-CAN injection을 시작한다. 기본
`receiver_lead_seconds`는 30초이므로 두 수신을 시작한 뒤 30초 이내에 injection을
시작해야 한다. 필요한 경우 Control PC 설정에서 이 값을 늘린 뒤 패키지를 다시 준비한다.

## B-CAN injection

B-CAN Control PC에서 먼저 preview한다.

```bash
python3 distributed_runner.py inject \
  --config distributed_node.yaml \
  --package experiment_0042_trial_0001.zip
```

두 RX가 실행 중임을 확인한 뒤 실제 송신한다.

```bash
python3 distributed_runner.py inject \
  --config distributed_node.yaml \
  --package experiment_0042_trial_0001.zip \
  --execute
```

Injection 직전에 B-CAN에서 정상 `0x366` payload를 다시 측정한다. 패키지를 만들 때의
baseline과 다르면 송신을 거부한다. B-CAN에서는 RX capture를 별도로 시작하지 않는다.

## 결과 회수 및 분석

P/I 명령이 끝나면 각각 다음 결과 ZIP이 생성된다.

```text
..._p_can_result.zip
..._i_can_result.zip
```

두 파일을 B-CAN Control PC로 복사한다. B-CAN에는 injection 후
`..._b_can_tx_result.zip`이 생성되어 있다. Control PC에서 다음을 실행한다.

카카오톡으로 전달할 때는 JSONL 내용을 복사해 붙이지 말고 결과 ZIP 파일 자체를 보낸다.
Control PC는 받은 ZIP의 압축을 풀지 않고 `pi_can_lab/distributed_results/`에 저장한다.
카카오톡이 파일명을 변경했다면 아래 명령의 경로만 실제 파일명에 맞게 지정하면 된다.

```bash
python3 distributed_runner.py analyze \
  --config distributed_runner.yaml \
  --package experiment_0042_trial_0001.zip \
  --tx-result distributed_results/experiment_0042_trial_0001_b_can_tx_result.zip \
  --p-result experiment_0042_trial_0001_p_can_result.zip \
  --i-result experiment_0042_trial_0001_i_can_result.zip
```

분석기는 패키지 digest, experiment/trial/mutation ID, 각 결과 파일 hash, 역할을 검증한다.
P/I 중 하나라도 없거나 캡처가 TX baseline/mutation 구간 전체를 포함하지 않으면 feedback을
저장하지 않는다. 성공하면 다음 파일을 보존한다.

```text
experiments/experiment_0042/trial_0001/
├── trial_plan.json
├── mutation.json
├── tx.jsonl
├── p_can.jsonl
├── i_can.jsonl
├── anomalies.json
├── feedback.json
└── metadata.json
```

## 다음 Trial

Trial 1 분석이 성공한 뒤 Control PC에서 `prepare`를 다시 실행한다. 동일 experiment ID를
사용하면 `feedback_state.json`의 완료된 Trial 1 결과가 Trial 2 mutation 선택에 반영된다.

```bash
python3 distributed_runner.py prepare \
  --config distributed_runner.yaml \
  --experiment-id 42 \
  --target-id 0x366 \
  --random-seed 366 \
  --mutation-profile all-0x366
```

분석되지 않은 Trial 패키지가 남아 있으면 다음 패키지 생성을 거부한다. 이 제약으로 현재
Trial의 부분 로그가 같은 Trial이나 다음 mutation에 잘못 반영되는 것을 방지한다.

## 한계

- 세 노트북/Pi가 공통 네트워크에 없으므로 결과 ZIP 전달과 Trial 시작은 수동이다.
- millisecond 단위 online adaptation은 불가능하며 이번 구조에서도 의도적으로 지원하지 않는다.
- cross-bus 시간 분석의 정확도는 세 Pi의 시스템 시각 동기화 품질에 의존한다.
- P/I 수신을 먼저 시작하고 설정된 lead 시간 안에 B injection을 시작해야 한다.
- 자동화가 필요해지면 추후 공유 폴더, 메시지 큐 또는 중앙 서버 전송 계층만 교체하면 되며,
  mutation·분석·feedback state 로직은 그대로 재사용할 수 있다.

## 가장 짧은 실행 순서

```text
1. Control PC: prepare
2. Trial ZIP을 P/I 담당자에게 전달
3. P/I 담당자: receive 실행
4. 두 담당자가 capture 시작을 알림
5. Control PC: inject --execute
6. P/I 담당자가 결과 ZIP을 카카오톡으로 전달
7. Control PC: 받은 ZIP을 distributed_results에 저장
8. Control PC: analyze
9. 완료 후 다음 Trial prepare
```
