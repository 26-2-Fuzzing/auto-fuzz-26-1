# Auto-Fuzz
> Real-vehicle ECU fuzzing framework with multi-monitoring and automated recovery

## CANoe paired 0x366 mutations

Windows CANoe와 WSL에서 mutation 방식을 선택해 CAN2로 송신하고 CAN1/2/3을 수집하는 절차는
[CANoe paired 실행 가이드](pi_can_lab/CANOE_PAIRED.md)를 참고하세요.
설치 의존성은 `pi_can_lab/requirements-canoe.txt`에 있습니다.
처음 준비한 뒤 저장소 루트에서 `bash pi_can_lab/canoe-run-wsl.sh -m signal_single -n 1 -x`를
실행하면 30초 참조 baseline 수집부터 paired 송신과 로그 저장까지 진행합니다.
저장소 위치는 스크립트가 자동으로 찾으며, CANoe에는 이 브랜치의
`pi_can_lab/canoe/config/ThreeChannelCAN.cfg`를 열어 둡니다.
campaign은 mutation 방식별 고정 폴더를 사용하므로 다음 묶음은
`--case-start`만 바꿔 실행합니다.

## 🚀 개발 환경 세팅
### 1. 요구사항
* 운영체제 : Linux (Ubuntu 20.04+ 권장) 또는 WSL2
* Python : **3.10 이상 권장**
* Git
* CAN 인터페이스 드라이버

### 2. 레포지토리 클론
```bash
git clone https://github.com/cxogus0822/auto-fuzz-26-1.git
cd auto-fuzz
```

### 3. 가상환경(venv) 생성 및 활성화
> 활성화되면 프롬프트에 `(.venv)`가 표시됩니다.
* Windows (CMD)
```bash
python -m venv .venv
.venv\Scripts\activate
```

* Windows (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

* macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. pip 최신화
```bash
python -m pip install --upgrade pip
```

### 5. 필수 라이브러리 설치
```bash
pip install -r requirements.txt
```

## ⚙️ Git 기본 규칙
* 기능 추가/수정은 반드시 **포크 (origin)** 저장소에서 브랜치를 생성 후,
* Pull Request (PR)로 원본 (upstream) `dev` 브랜치에 병합합니다.
* 코드 리뷰와 테스트 통과 후에만 `upstream/dev`에 반영됩니다.

### 1. 최초 세팅 시
#### 1️⃣ upstream 등록
```bash
git remote add upstream //github.com/cxogus0822/auto-fuzz-26-1.git
```

#### 2️⃣ 확인
* `origin` (내 포크) + `upstream` (원본) 둘 다 보여야 정상
```bash
git remote -v
```

#### 3️⃣ 원본 최신 dev 가져오기
```bash
git pull upstream dev
```
#### 4️⃣ 내 dev를 원본과 동일하게 맞추기
* reset, 기록 덮어쓰는 과정
```bash
git checkout dev
git reset --hard upstream/dev
git push origin dev --force
```

### 2. 브랜치 생성 및 이동
```bash
git switch -c feature/기능명
```

#### 🌿 브랜치명 규칙
* feature/기능명 (새 기능)
* bugfix/이슈번호-설명 (버그 수정)
@@ -86,7 +69,7 @@

**Ex.** `feature/github-api-integration`, `bugfix/34-filter-extension-error`, `docs/update-readme`

### 3. 코드 수정 → 변경사항 저장 (commit)
```bash
git add "경로/파일이름.py"
git commit -m "Add: GitHub API 연동 기능 추가"
```
  * `Test` (테스트)
  * `Chore` (환경/설정)

### 4. GitHub에 업로드 (push)
```bash
git push origin feature/기능명
```

### 5. Pull Request(PR) 생성
> GitHub에서 **Compare & pull request** 버튼 클릭

#### 📌 PR 설정 및 Comment 작성
* base: `upstream/dev`
* compare: `origin/feature/기능명`
* 제목/설명 작성 → **Create pull request**

### 6. upstream/dev 최신 반영하기 (내 브랜치 갱신)
> 작업 중간이나 PR 직전에 최신 dev를 반영하세요.

```bash
git fetch upstream
git checkout feature/기능명
git merge upstream/dev
```

## ✅ 가상환경 사용하기 (venv)
* 모든 작업은 가상환경 안에서 진행해 주세요.
* 각자 다른 시스템 설정으로 인한 충돌을 방지할 수 있습니다.

## 📥 의존성 설치
> 🔁 프로젝트 클론 후 또는 .env 등 환경 구성 후에 꼭 실행하세요.
```bash
pip install -r requirements.txt
```

## 🗂️ 라이브러리 설치 시 주의
* 새 패키지를 설치했다면 `requirements.txt`에 반영해 주세요.
```bash
pip install some-library
pip freeze > requirements.txt
```

* ⚠️ `pip freeze > requirements.txt`는 현재 가상환경의 모든 패키지를 덮어씁니다. 
* 새 패키지만 반영하려면 아래처럼 일부만 추출해서 추가하세요.
```bash
pip freeze | grep some-library >> requirements.txt
```
