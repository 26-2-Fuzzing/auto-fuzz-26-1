# CANoe Mutation Sender

`canoe_fuzzer.py`는 기존 `A5BlinkmodiMutator`의 0x366 mutation catalogue를
재사용해 CANoe로 송신하는 전용 도구다. `experiment_runner.py`의 SSH 수집,
paired/no-op 실행, 로그 분석 및 feedback은 포함하지 않는다.

## 실행 명령

CANoe COM 자동화는 Windows Python에서 실행해야 한다. 공통 설정은
`canoe_fuzzer.yaml`에 있으므로 평소에는 mutation 방법만 고르면 된다.

```powershell
python canoe_fuzzer.py -m signal_single
python canoe_fuzzer.py -m signal_single -x
```

Windows용 실행 래퍼를 사용하면 더 짧게 실행할 수 있다.

```powershell
.\canoe-fuzz.cmd -m signal_single -x
```

- 첫 번째 명령은 preview만 수행한다.
- `-x` 또는 `--execute`가 있을 때만 CANoe로 송신한다.
- YAML의 기본 방법을 그대로 사용하면 `python canoe_fuzzer.py -x`로 실행한다.
- 송신 case 수만 바꾸려면 `-n 3`처럼 지정한다.

기존 명령과의 대응은 다음과 같다.

| 기존 옵션 | CANoe sender |
|---|---|
| `--cycle-family signal_single` | `-m signal_single` |
| `--cycle-max-sets 10` | YAML `mutation.max_cases: 10` 또는 `-n 10` |
| `--target-id 0x366` | YAML `target.id` |
| `--source-bus B_CAN` | YAML `target.source_bus` |
| `--random-seed 366` | YAML `mutation.random_seed` |
| `--execute` | `-x` |

지원하는 catalogue family는 아래 명령으로 확인한다. 단, 현재 DBC와 baseline에서
실제로 생성 가능한 case가 없는 family는 preview 단계에서 안전하게 중단된다.

```powershell
python canoe_fuzzer.py --list-methods
```

- `signal_single`
- `signal_combination`
- `state_contradiction`
- `undefined_enum`
- `undefined_bit_single`
- `undefined_bit_multi`
- `defined_undefined_mix`
- `temporal_sequence`

현재 저장소의 `A5.dbc`와 기본 payload 조합에서는 `undefined_enum`에 해당하는
safe/distinct case가 없으므로 이 방법만 송신 계획을 만들지 않는다. 나머지 7개
family는 기본 설정에서 생성되는 것을 단위 테스트로 확인한다.

## CANoe 설정

1. CANoe **Measurement Setup의 transmit/real-time branch**에 CAPL P-Block을
   만들고 `canoe/AutoFuzzTx.can`을 연결한다.
2. CAPL Browser에서 수동 컴파일이 성공하는지 한 번 확인한다. 실행 시에도
   `compile_capl: true`가 전체 CAPL node를 다시 컴파일하고 결과를 검사한다.
3. CANoe hardware configuration의 CAN channel 번호를 확인한다.
4. `canoe_fuzzer.yaml`의 `canoe.channel`을 같은 번호로 설정한다.
5. CANoe 애플리케이션은 열어 두되 **Measurement는 정지 상태**로 둔다.

Python은 COM으로 다음 CAPL 함수를 호출한다.

```text
AutoFuzzSend(channel, canId, dlc, word0, word1, word2, word3)
```

각 `word`에는 payload 두 바이트가 little-endian으로 들어간다. CAPL 함수가
`output()`으로 Classic CAN frame을 송신하며 반환값은 사용하지 않는다. Python의
호출 성공은 CAPL 호출이 예외 없이 전달되었다는 뜻일 뿐, 물리 버스의 CAN ACK나
ECU 수신을 확인했다는 뜻은 아니다.

`CAPL.GetFunction()`은 CANoe 규약상 Measurement의 `OnInit` event 안에서만
획득할 수 있다. 따라서 도구는 event handler를 먼저 등록한 뒤 Measurement를
시작하고, CAPL 함수까지 준비된 후에만 mutation 송신을 시작한다. 이미 Measurement가
실행 중이면 기본값에서는 중단한다. 도구가 정지 후 재시작해도 되는 전용 설정이라면
`restart_running_measurement: true`를 명시할 수 있다.

## 설정 파일

주로 수정할 값은 다음과 같다.

```yaml
mutation:
  method: signal_single
  max_cases: 10
  duration_seconds: 1.0
  interval_ms: 50.0

canoe:
  channel: 1
  attach_existing: true
  compile_capl: true
  start_measurement: true
  restart_running_measurement: false
  stop_measurement_on_close: true
  capl_function: AutoFuzzSend
```

특정 CANoe configuration을 Python이 열어야 하면 다음 값을 함께 지정한다.

```yaml
canoe:
  configuration_path: "C:/path/to/AutoFuzz.cfg"
  open_configuration: true
```

## 설치

Windows 가상환경에서 의존성을 설치한다.

```powershell
py -3 -m venv .venv-win
.\.venv-win\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

WSL/Linux와 같은 checkout을 함께 쓰더라도 가상환경 형식이 충돌하지 않도록
Windows 환경은 `.venv-win`을 사용한다. 실행 래퍼는 `.venv-win`, Windows 형식의
`.venv`, `py -3`, `python` 순서로 인터프리터를 찾는다. `pywin32`는 Windows에서만
설치되며 Preview와 단위 테스트에서는 실제 CANoe가 필요하지 않다.

저장소가 WSL 파일시스템에 있어 Windows 가상환경을 저장소 밖에 두어야 한다면
해당 Python 경로를 `AUTOFUZZ_PYTHON`으로 지정할 수 있다.

```powershell
$env:AUTOFUZZ_PYTHON = "$env:LOCALAPPDATA\auto-fuzz-canoe-venv\Scripts\python.exe"
.\canoe-fuzz.cmd -m signal_single -n 1
```

로컬 회귀 테스트는 다음처럼 실행한다.

```powershell
python -m unittest test_canoe_fuzzer.py test_paired_cycle.py test_a5_0x366_mutator.py
```

## 현재 송신 계약

- CAN ID: Standard CAN `0x000`~`0x7FF`
- Payload: Classic CAN 0~8 bytes
- 현재 mutation target: `0x366`, 8 bytes
- 기본 안전 간격: 50 ms 이상
- case 종료 후 원본 payload 1회 복원
- 실제 송신은 명시적인 `-x`가 있을 때만 허용

CAN FD와 Extended CAN ID는 현재 범위에 포함하지 않는다.
