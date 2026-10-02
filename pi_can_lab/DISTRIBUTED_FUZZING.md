# 분리된 3개 노트북/Pi 환경 실행 가이드

현재 stage 1 예시는 `feedback.enabled: false`, 30/10/1/20초 phase, 50 ms 송신 간격입니다.
분석 결과는 후보 관측이며 자동 검증·exploitation 근거가 아닙니다. no-op 대조와 오프라인
오탐 검증 절차는 [`CALIBRATION.md`](CALIBRATION.md)를 참고하십시오. 기존
`experiment_0001` 로그만으로 현장 false-positive rate를 주장하지 않습니다.

## 구조

네트워크 문제로 한 노트북이 세 Raspberry Pi에 동시에 접속하지 못할 때 사용한다.

```text
B-CAN 노트북(Control PC) ─ SSH → B-CAN Pi: mutation 생성 및 injection/TX 저장
P-CAN 노트북             ─ SSH → P-CAN Pi: RX 저장만 수행
I-CAN 노트북             ─ SSH → I-CAN Pi: RX 저장만 수행
```

B-CAN RX는 수집하지 않는다. 한 Trial의 분석 입력은 `B-CAN TX + P-CAN RX + I-CAN RX`이다.
현재 Trial 실행 중 mutation을 바꾸지 않으며, 두 RX 결과와 TX 결과가 Control PC에 모두
모인 뒤에만 관측 결과를 기록한다. 자동 feedback 선택은 stage 1에서 비활성화돼 있다.

권장 단위는 mutation과 원본 송신 no-op을 짝지은 세트다. 단, 세 노트북이 서로를 제어할
네트워크가 없으므로 로컬 runner처럼 한 명령으로 두 구간을 연속 실행할 수는 없다.
첫 구간의 결과 ZIP을 모두 회수·분석하고 recovery 복귀를 확인한 후에만 두 번째
패키지를 만들 수 있다. mutation은 첫 패키지를 만들 때 고정되며 각 injection 직전
B-CAN 원본을 재측정한다. 순서는 세트마다 교대된다.

## 권장: 짝 세트 진행

B-CAN Control PC에서 첫 패키지를 만든다. `--base-payload` 또는 설정의
`target.reference_payload`가 필요하고, 실제 송신 직전 live probe가 이 값과 같아야 한다.

```bash
python3 distributed_runner.py prepare-pair \
  --config distributed_runner.yaml --experiment-id 42 \
  --target-id 0x366 --random-seed 366 --mutation-profile all-0x366
```

출력된 첫 ZIP으로 아래의 `receive → inject --execute → 결과 ZIP 회수 → analyze`를
한 번 완료한다. 첫 recovery가 원래 상태로 돌아왔다는 증거가 충분해야 두 번째
패키지를 만들 수 있다. 부족하거나 첫 Trial이 미완료면 중단하며 자동 재시도하지 않는다.

```bash
python3 distributed_runner.py prepare-pair-next \
  --config distributed_runner.yaml \
  --first-package experiment_0042_trial_0001.zip
```

출력된 둘째 ZIP에 대해 동일한 네 단계의 수집·송신·분석을 반복한 다음, 두 패키지를
짝 대조한다.

```bash
python3 distributed_runner.py analyze-pair \
  --config distributed_runner.yaml \
  --first-package experiment_0042_trial_0001.zip \
  --second-package experiment_0042_trial_0002.zip
```

실제 패키지 파일명은 출력에 맞춘다. 짝 보고서는 원본·사전 상태·TX 시간 패턴·RX 및
시계 품질을 확인한다. 이 구성에서는 B-CAN RX가 없어 30초 baseline 동안 원본
payload가 계속 안정적이었는지 증명할 수 없다. 따라서 첫 recovery의 P/I 상태와
B-CAN TX restore가 충분하면 둘째 패키지 준비는 가능하지만, 최종 짝 보고서의
`comparability`는 원본 사전 상태 미관측으로 `inconclusive`를 유지한다.
P/I 후보 목록은 진단 자료로 남기되 `mutation-only` 인과 판정이나 자동 exploitation은
하지 않는다. 노트북들의 공통 시계 기준도 검증되지 않았다면 그 불확실성도 추가된다.
완전한 짝 비교가 필요하면 B-CAN RX 동시 캡처를 수집 경로에 추가해야 한다.
다음 세트는 이전 세트의 **두 recovery가 모두 안정적**이어야 준비할 수 있다.
이전 보고서가 B-CAN 원본 baseline 미관측만으로 보류된 경우, 장비 상태를 별도로
확인한 운영자가 `prepare-pair --acknowledge-unverified-source-baseline`을 명시해야
다음 세트를 준비할 수 있다. 다른 상태/품질 실패는 이 옵션으로 우회되지 않는다.

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

세 Pi와 세 노트북의 시각은 NTP/chrony로 동기화되어야 한다. 코드는 시스템 시각을 변경하지 않는다.
각 결과의 clock offset은 Pi와 그 Pi에 접속한 노트북 사이의 값이다. 노트북들이 검증된 동일
시계 기준을 공유할 때만 세 `distributed_node.yaml`에 같은 `node.clock_reference_id`를
지정한다. 기준을 확인하지 못했다면 이 값을 비워 둔다. 그 경우 교차 호스트의 시간 정렬과
이상반응 귀속은 보류된다.

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

기존 단독 `prepare`도 유지한다. 원본 payload를 같은 송신 구간에 넣어 판정기의
오탐을 측정하려면 별도 Trial을 `prepare --control-noop`으로 생성할 수 있지만,
이는 위의 짝 세트와 달리 공통 mutation·상태 복귀가 보장되지 않는다. 대조 결과는
mutation 이력이나 다음 mutation 선택에 반영되지 않는다.

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
사용하면 `feedback_state.json`의 완료된 결과를 읽는다. 현재 자동 feedback은 꺼져 있으며,
후보가 다른 Trial에서 반복되더라도 자동 검증·집중 탐색·exploitation에는 사용하지 않는다.
원본과 상태가 맞는 no-op 대조 및 독립 반복을 별도로 검토한다.

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
