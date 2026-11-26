# CANoe paired 0x366 mutation 실행

이 가이드는 WSL 명령으로 Windows CANoe에 연결해 CAN `0x366`의 선택한 mutation
방식을 실행하고, 변이 측정과 no-op 대조 측정을 각각 기록하는 방법이다. 명령의
경로는 각 PC의 설치 위치에 맞춰 지정한다. 현재 검증된 실행 경로는 CAN2
송신과 CAN1·2·3 수신으로 고정돼 있다.

## 준비

- Windows에 CANoe와 Python을 설치하고, 저장소를 WSL에서 접근할 수 있는
  Windows 드라이브(`/mnt/c/...` 등)에 둔다.
- `pi_can_lab/canoe/config/ThreeChannelCAN.cfg`를 CANoe에서 연다. 함께 제공된
  CAPL·시스템 변수·로깅 구성 요소의 참조 경로를 해당 PC에 맞게 확인하고
  CAPL이 동작하는지 확인한다. 저장소에는 빌드 결과물인 `FuzzHarness.cbf`가
  포함되지 않는다. CANoe가 자동으로 준비해 `--probe-only` 검증이 통과하면
  수동 빌드는 필요 없고, 컴파일 오류나 heartbeat 오류가 나면
  `FuzzHarness.can`을 CANoe에서 빌드한다. 세 CAN 채널의 하드웨어 매핑,
  Real Bus·Online Mode, BLF·ASC 로깅과 파일명 자동 증가를 확인한다.
  측정은 정지해 둔다.
  이 설정은 CANoe 20.0.195에서 저장됐다. 다른 버전에서 열면 설정 변환이나
  오래된 Vector 공용 템플릿 경로에 대한 경고를 확인한다.
- 제공 압축본의 동명 CFG·CAPL 소스·시스템 변수는 저장소 파일과 내용이 같다.
  어느 복사본을 열든 `cfg` 변수와 `--canoe-config`에는 CANoe에서 **실제로
  열어 둔 CFG 경로**를 넣는다. CFG 파일 경로가 다르면 실행기가 중단한다.
- 아래 예시의 `channel=2`는 대상 `0x366`을 수신·송신할 CANoe 채널이다.
  실제 배선과 CANoe 채널 매핑을 **CAN2 기준으로 맞춘다**. 실행기는 현재
  `--channel 2`만 허용한다. 다른 두 채널도 수신 상태여야 한다. 실차 시험은
  정차한 통제 환경에서 진행한다.
- `pi_can_lab/canoe_paired.yaml`의 DBC 경로와 기본 mutation 설정을 확인한다.
  제공된 `A5.dbc`를 사용하면 상대 경로 `../A5.dbc`를 유지할 수 있다.

다음은 **WSL**에서 실행한다. 첫 `cd`만 각자의 저장소 위치로 바꾼다.
`py.exe`가 WSL에서 보이지 않으면 Windows Python Launcher의 실행 경로를
지정해 같은 명령을 실행한다.

```bash
cd "/mnt/c/path/to/your/checkout"
repo="$PWD"
cfg="$repo/pi_can_lab/canoe/config/ThreeChannelCAN.cfg"
channel=2
method=signal_single  # 아래 지원 방식 중 원하는 이름으로 변경
mkdir -p "$repo/pi_can_lab/canoe/logs/capture"

py.exe -3 -m venv "$(wslpath -w "$repo/.venv-win")"
"$repo/.venv-win/Scripts/python.exe" -m pip install -r \
  "$(wslpath -w "$repo/pi_can_lab/requirements-canoe.txt")"
```

## mutation 방식 선택

`method`에 `signal_single`, `signal_combination`, `state_contradiction`,
`undefined_enum`, `undefined_bit_single`, `undefined_bit_multi`,
`defined_undefined_mix`, `temporal_sequence` 중 하나를 넣는다. 카탈로그의
실행 가능한 case 수는 방식별로 다르며 미리보기의 `[PAIRED PLAN]`에 표시된다.
제공된 `A5.dbc`와 기본 payload 기준 case 수는 다음과 같다.

| 방식 | case 수 |
| --- | ---: |
| `signal_single` | 122 |
| `signal_combination` | 4 |
| `state_contradiction` | 13 |
| `undefined_enum` | 53 |
| `undefined_bit_single` | 26 |
| `undefined_bit_multi` | 71 |
| `defined_undefined_mix` | 42 |
| `temporal_sequence` | 12 |

같은 송신 payload가 서로 다른 방식에도 속할 수 있으므로 방식별 case를 단순히
합쳐 고유 변이 수로 해석하지 않는다. case 번호는 방식마다 1번부터 시작하며,
**방식마다 별도 campaign 디렉터리**를 쓴다.
이전 `CANoe` 직접 송신 카탈로그는 방식 간 중복도 제외했지만, `CANoe2`는
선택한 방식 내부의 중복만 제외한다. 변이 생성 규칙과 DBC는 동일하다.
예를 들어 이전에 `signal_single`과 겹쳐 실행 목록에서 빠졌던
`undefined_enum` 53개가 이제 해당 방식의 case로 선택된다.

## 기준 baseline 준비

첫 실행에는 완료된 **BLF와 ASC 한 쌍**이 필요하다. CANoe에서 대상 차량 상태를
유지한 채 측정을 시작해 약 30초 동안 수신만 기록하고 측정을 중지한다.
자동 증가로 생성된 동일 이름의 `.blf`와 `.asc`가 모두 있고, ASC 끝에
`End TriggerBlock`이 기록됐는지 확인한다. 그 BLF의 경로를 `baseline`에
입력한다. 이 파일에서 지정 채널의 `0x366` 수신 payload를 선택하며,
case마다 새로운 30초 baseline도 별도로 기록한다.

```bash
baseline="/mnt/c/path/to/your/reference-baseline.blf"
campaign="$repo/pi_can_lab/logs/canoe_paired/${method}_$(date -u +%Y%m%dT%H%M%SZ)"
```

같은 campaign을 이어서 실행할 때는 **같은 `cfg`, `baseline`, `campaign`,
DBC, YAML 설정, mutation 방식**을 사용한다. 새 baseline이나 DBC를 선택했다면 새 campaign
디렉터리를 만든다.

이 브랜치의 변이 목록 해시는 DBC 내용과 case 설정을 기준으로 계산하므로
저장소 경로가 다른 PC에서도 같은 목록을 식별한다. 이전 `CANoe` 브랜치에서
만든 campaign은 경로와 구형 목록 해시에 묶여 있으므로 이 브랜치에서
이어 쓰지 않고 새 campaign으로 시작한다. 기존 로그는 그대로 보존한다.

## 미리보기와 무송신 확인

먼저 case 범위를 계획한다. 아래 미리보기는 CANoe 측정을 시작하지 않는다.
`-n`은 이번 묶음의 case 수이며 1~20을 지정할 수 있다.

```bash
bash "$repo/pi_can_lab/canoe-paired-wsl.sh" \
  --channel "$channel" --canoe-config "$cfg" \
  --reference-baseline-log "$baseline" --campaign-dir "$campaign" \
  --case-start 1 -n 20 -m "$method"
```

송신 전 6초 수신 확인을 별도로 수행하려면 같은 명령에 `--probe-only`를
붙인다. 이 측정은 CANoe 송신 요청을 하지 않고 CAN1·2·3의 수신,
대상 채널 `0x366` 원본 payload, BLF·ASC 완결성과 오류 프레임을 확인한다.

```bash
bash "$repo/pi_can_lab/canoe-paired-wsl.sh" \
  --channel "$channel" --canoe-config "$cfg" \
  --reference-baseline-log "$baseline" --campaign-dir "$campaign" \
  --case-start 1 -n 20 -m "$method" --probe-only
```

## paired 실행과 이어서 실행

미리보기 범위와 차량 상태를 확인한 뒤 `-x`를 지정하면 실제 송신을 시작한다.
한 case는 mutation과 no-op를 각각 독립된 CANoe 측정으로 실행한다.

| 각 측정의 구간 | 시간 | 대상 채널 `0x366` 송신 요청 |
| --- | ---: | ---: |
| 사전 수신 확인 | 6초 | 0건 |
| baseline | 30초 | 0건 |
| normal | 10초 | 원본 200건 |
| mutation 또는 no-op slot | 1초 | 선택한 방식의 변이 또는 원본 10~20건 |
| recovery | 20초 | 원본 1건 |

한 case의 두 측정 시간은 최소 134초이며, CANoe 시작·종료와 로그 검증 시간이
더해진다. 정적 방식은 slot에 20건을 50ms 간격으로 보내 한 case의 대상 채널
송신 요청이 총 442건이다. `temporal_sequence`는 여러 payload를 50ms 또는
100ms 간격으로 순환하며, 100ms case는 slot 10건씩으로 총 422건이다. no-op는
같은 개수와 간격으로 원본 payload를 보낸다. 측정 중에는
문·창문·키·점화 상태를 일정하게 유지한다.

```bash
bash "$repo/pi_can_lab/canoe-paired-wsl.sh" \
  --channel "$channel" --canoe-config "$cfg" \
  --reference-baseline-log "$baseline" --campaign-dir "$campaign" \
  --case-start 1 -n 20 -m "$method" -x
```

1~20번이 모두 `[PAIR DONE]`으로 끝났고 해당 방식에 21번 이후 case가 있다면
다음 묶음은 다음과 같다. 실제 전체 case 수는 `[PAIRED PLAN]` 출력에서 확인한다.

```bash
bash "$repo/pi_can_lab/canoe-paired-wsl.sh" \
  --channel "$channel" --canoe-config "$cfg" \
  --reference-baseline-log "$baseline" --campaign-dir "$campaign" \
  --case-start 21 -n 20 -m "$method" -x
```

중간 번호부터 재개할 때는 시작 번호와 이번 묶음의 개수를 바꾼다. 예를 들어
3~20번을 실행하려면 **같은 방식의 같은 campaign에서 1·2번이 완료됐는지**
먼저 확인하고 다음 명령을 쓴다.

```bash
start=3
count=18
bash "$repo/pi_can_lab/canoe-paired-wsl.sh" \
  --channel "$channel" --canoe-config "$cfg" \
  --reference-baseline-log "$baseline" --campaign-dir "$campaign" \
  --case-start "$start" -n "$count" -m "$method" -x
```

완료된 case를 다시 선택하면 저장된 결과를 검증한 뒤 송신 없이 건너뛴다.
중간에 실패한 case는 `error.json`과 원본 BLF·ASC를 보존하며 자동으로
재송신하지 않는다. 실패가 나면 원인을 확인하기 전 다음 case 번호로
넘어가지 않는다. `--exclude-interrupted-case 2`는 특정 중단 사례를 위한
검증 절차이므로 일반적인 재시도 옵션으로 사용하지 않는다.

## 로그와 결과

로그는 `campaign` 아래의 `case_####/episode_*_mutation/` 및
`case_####/episode_*_noop/`에 저장된다. 각 episode에는 6초 무송신 로그
`probe/raw/*.blf`·`*.asc`와 본 측정 로그 `capture/raw/*.blf`·`*.asc`가
있다. **한 ASC 안에 CAN1·2·3 수신 기록이 함께 들어가며**, 선택한 채널의
송신 기록도 함께 들어간다. `plan.json`, `phases.jsonl`, `episode.json`,
`complete.json`에서 계획과 검증 결과를 확인한다. 원본 로그를 삭제하거나
완료되지 않은 case의 이름을 바꾸지 않는다.

`[PAIR DONE]`과 `complete.json`은 요청한 프레임이 CANoe 로그에 기록되고
각 측정의 수신·시간·복구 조건이 검증됐다는 뜻이다. ECU 반응이나 변이 효과의
판정은 별도 오프라인 비교가 필요하며, `analysis_status`에 진행 상태가
기록된다.
