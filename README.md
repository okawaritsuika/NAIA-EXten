# NAIA 추가 편의기능

NAIA 2.0에 사용하면서 필요한 편의성 기능을 하나씩 추가하기 위한 **종합 Custom Extension** 골격입니다.

이 저장소 구조는 NAIA **v2.0.33 / `naia_ext_api=1`**의 공식 Extension 시스템을 기준으로 합니다.

## GitHub 설치용 저장소 구조

```text
NAIA-EXten/
├─ extension.json
├─ main.py
├─ README.md
├─ FEATURE_TEMPLATE.py.txt
└─ naia_exten/
   ├─ __init__.py
   ├─ extension.py
   ├─ feature_manager.py
   ├─ patch_manager.py
   ├─ host_bridge.py
   └─ features/
      ├─ __init__.py
      ├─ base_feature.py
      └─ example_status.py
```

`extension.json`은 **GitHub 저장소 루트**에 두는 것을 권장합니다.

## NAIA에서 설치

1. 이 골격을 새 GitHub 저장소 루트에 그대로 올립니다.
2. NAIA 실행.
3. **Settings ▸ Extension**으로 이동.
4. 저장소 URL 입력:
   `https://github.com/<owner>/<repo>`
5. **GitHub에서 설치** 클릭.
6. 설치 후 목록에 `NAIA 추가 편의기능`이 **미승인** 상태로 나타납니다.
7. 신뢰 경고를 확인하고 승인/활성화합니다.
8. 메인 UI의 EXten 퀵 버튼에서 기능 설정을 엽니다.

NAIA v2.0.33 설치기는 GitHub repository ZIP을 받아 `extension.json`을 찾고,
`user-data/extensions/naia_exten/` 아래로 복사합니다. git 설치는 필요 없습니다.

## 첫 실행 확인

정상 로드 시 콘솔에:

```text
[ext:naia_exten] ready — 1 feature(s) discovered
```

형태의 로그가 표시됩니다.

퀵 팝업에는 기본 OFF인:

- `골격 테스트 활성화`

가 나타납니다.

켜면 `테스트 실행` 버튼이 나타납니다. 버튼을 누르면 로그와 토스트가 표시됩니다.
이 기능은 실제 생성 동작을 변경하지 않습니다.

골격 확인이 끝나면:

```text
naia_exten/features/example_status.py
```

를 삭제해도 됩니다.

## 앞으로 기능 추가 규칙

작은 기능:

```text
naia_exten/features/my_feature.py
```

큰 기능:

```text
naia_exten/features/my_feature/
├─ __init__.py
├─ feature.py
├─ logic.py
└─ ...
```

`FeatureManager`는 package 내부 `feature.py`도 탐색할 수 있게 되어 있습니다.

각 기능은 `BaseFeature`를 상속합니다.

## 공식 API 우선 원칙

NAIA v2.0.33이 보장하는 공식 표면은 `register(ctx)`의 `ctx`입니다.

가능하면 기능은 다음 API로 구현합니다.

- `ctx.subscribe(...)`
- `ctx.register_hook(...)`
- `ctx.register_panel(...)`
- `ctx.enqueue_generation(...)`
- `ctx.start_generation_queue()`
- `ctx.cancel_generation(...)`
- `ctx.get_current_request()`
- `ctx.get_api_mode()`
- `ctx.get_sampler_options()`
- `ctx.show_toast(...)`
- `ctx.get_result_image(...)`
- `ctx.get_save_directory()`
- `ctx.resolve_nai_characters()`
- `ctx.load_settings(...)`
- `ctx.save_settings(...)`
- `ctx.log(...)`

## 기존 NAIA 기능을 직접 개선해야 할 때

공식 API로 해결할 수 없는 기능만 `HostBridge` + `PatchManager`를 사용합니다.

```text
feature
   ↓
HostBridge
   ↓
NAIA private/internal API
```

NAIA 내부 구조가 바뀌었을 때 개별 feature 전체를 고치는 대신 `host_bridge.py`
또는 별도 compatibility adapter에서 차이를 흡수하기 위한 구조입니다.

`ctx._app_context` 같은 접근은 v2.0.33에서 존재하지만 **공식 API가 아니므로**
기능 파일에서 직접 사용하지 않는 것을 원칙으로 합니다.

## 의존성

현재 골격은 외부 Python 패키지가 필요 없습니다.

추후 필요하면 `extension.json`에 NAIA의 공식 dependency 형식으로 선언합니다.

```json
"python": {
  "requirements": ["rapidfuzz>=3.9"],
  "max_install_mb": 200
}
```

런타임에서 직접 pip를 실행하지 않습니다.

## 개발 핫리로드 (v0.2.0)

`NAIA 추가 편의기능` 패널의 Development 섹션에 다음 항목이 추가됩니다.

- **핫픽스 자동 반영**: 기본 ON. `naia_exten/features/**/*.py` 변경 저장을 감지해 약 1초 뒤 자동 재등록합니다.
- **↻ Exten 지금 다시 불러오기**: 저장 감지를 기다리지 않고 feature들을 즉시 재등록합니다.

핫리로드 순서는 `unregister() → PatchManager 원복 → 새 feature import → register() → 패널 갱신`입니다.
문법/import/register 오류가 발생하면 이전 feature 모듈/patch로 롤백합니다.

`NAIA 추가 편의기능` 패널에서는 확장 UI 노출과 각 기능의 **작동 토글·액션**을 관리합니다.
Settings ▸ Extension의 확장 ON/OFF는 퀵 버튼과 주입 UI의 표시만 제어하며, 퀵 팝업의
`Activate This Script` 또는 각 기능 UI의 OFF는 기능 동작만 멈추고 해당 UI는 유지합니다.
세부 설정은 각 기능의 전용 Prompt/Search UI에서 관리하며, Comic Maker는 별도 활성화 토글
없이 패널의 액션으로 실행합니다.

현재 핫리로드 대상은 **feature 폴더**입니다. `main.py`, `extension.py`, `feature_manager.py`,
`host_bridge.py`, `patch_manager.py` 같은 EXten 코어 자체를 바꾼 경우에는 NAIA를 한 번 재시작해야 합니다.

## v0.2.1 - Search 가이드 Parquet 토글

- Search 모듈 상단 `ⓘ 가이드`에 **Parquet 실시간 동기화 활성화** 스위치 1개만 표시합니다.
- 이 기능은 Prompt/Search의 스위치에서만 켜고 끄며, 추가 편의기능 패널에는 중복 토글을 표시하지 않습니다.
- NAIA 본체의 `app.js` 파일은 수정하지 않습니다. Extension이 `/app.js` 응답을 런타임 패치해 UI 브리지를 주입합니다.
- feature 파일 교체는 핫리로드로 즉시 반영됩니다. 다만 이미 브라우저/Electron에 로드된 JS는 교체할 수 없으므로 **UI 코드 변경 후에는 Ctrl+R 한 번**이 필요합니다. NAIA 전체 재시작은 필요하지 않습니다.


## GSQE 확률 분배

`Tag Filter`의 Rating(G/S/Q/E) 아래에 100% 확률 바를 추가합니다. 세 경계를 드래그해 G/S/Q/E 비율을 조절하며, 랜덤 프롬프트는 parquet 행 개수 비율이 아니라 설정한 rating 비율을 먼저 적용합니다. Rating 버튼이 꺼진 등급이나 남은 행이 없는 등급은 후보에서 제외되고 나머지 활성 등급끼리 자동 정규화됩니다.


## v0.2.8 - 다중 Parquet 균등 확률

Search의 `다중 Parquet 랜덤 풀`에 **Parquet별 균등 확률** 토글을 추가합니다.

- OFF: 선택한 parquet을 합친 전체 행 수 기준 랜덤(행이 많은 파일이 더 자주 선택됨).
- ON: parquet 파일을 먼저 동일 확률로 선택한 뒤 해당 파일 안에서 랜덤 행을 선택합니다. 예: A=10만행, B=100만행이어도 A/B는 50%/50%.
- GSQE 확률 분배가 켜져 있어도 균등 모드에서는 parquet 선택을 먼저 수행하고, 선택된 parquet 내부에서 GSQE 비율을 적용합니다.
- Tag Filter 활성 상태에서도 동일한 source-first 선택을 사용합니다.
- 토글 설정은 Extension settings에 저장되며 재실행 후에도 유지됩니다.


## v0.2.9
- Fix: multi_parquet_pool no longer double-patches SearchResultModel pop methods already owned by parquet_live_sync.
- Restores Search UI for multi-Parquet selection and the equal-probability toggle.


## v0.3.0 - PromptServer 랜덤 상황 결합

`Prompt Engineering` 팝업 하단에 **서버 랜덤 프롬프트** 설정을 추가합니다.

- 서버는 `http://127.0.0.1:8765`의 PromptServer를 사용합니다.
- 프리셋은 전체 프리셋 중 랜덤, `free`(선택 안 함), 특정 서버 프리셋을 선택할 수 있습니다.
- 남성/여성 수로 `/api/scenarios/random`을 조회하고, 받은 `base_prompt`를 현재 랜덤 프롬프트 뒤에 붙입니다.
- Character Prompt의 독립 `boy`/`girl` 계열 태그를 기준으로 `maleN`/`femaleN` 응답을 각 캐릭터 프롬프트 뒤에 순서대로 붙입니다. `cowgirl`, `tomboy` 같은 부분 문자열은 성별 태그로 취급하지 않습니다.
- 서버가 꺼져 있거나 조건에 맞는 상황이 없으면 서버 결합만 건너뛰고 NAIA의 기존 랜덤 생성은 계속합니다.
- Prompt Engineering UI 코드를 처음 반영할 때는 **Ctrl+R 한 번**이 필요합니다.

## v0.3.1 - PromptServer 사용 상태 API 대응

- `/api/scenarios/random` 요청에 `mark_used`와 `include_used`를 명시하며 deprecated `exclude_used`는 사용하지 않습니다.
- 기본값은 미사용 상황만 조회하고 반환된 상황을 사용 처리하는 기존 동작입니다.
- Prompt Engineering에서 **조회 시 사용 처리**와 **사용한 상황 포함**을 각각 설정할 수 있습니다.
- 조건에 맞는 미사용 상황이 없어 발생하는 HTTP 404는 정상적인 후보 소진으로 처리합니다.

## v0.3.2 - PromptServer 결합 및 Character wildcard 공유 수정

- PromptServer의 `/api/presets` 목록을 Prompt Engineering 프리셋 선택기에 표시하며 서버 지연 시작도 재시도합니다.
- 서버 `base_prompt`를 최종 일반 Prompt의 postfix에 추가하고 포함된 wildcard도 일반 Prompt 범위에서 전개합니다.
- 서버가 Character prompt 뒤에 붙인 wildcard도 같은 그림의 Character prompt/UC와 동일한 공유 cache를 사용합니다.
- Character 동일 wildcard 공유는 일반/base Prompt에는 적용하지 않으며 generation 종료 후 cache를 제거합니다.

## v0.4.0 - PromptServer Comic Maker

- PromptServer의 구조화된 `ComicPlan`을 `/api/comics/random`에서 가져옵니다.
- 페이지 해상도, 페이지 수, 남녀 인원수, Comic preset, 언어, 텍스트 방식을 조건으로 조회합니다.
- 패널을 `order` 순서로 생성하고 정규화된 `rect` 좌표에 맞춰 정확한 페이지 해상도로 합성합니다.
- `overlay` 모드는 NAI 생성물에서 텍스트를 제외하고 한국어 말풍선과 효과음을 PIL로 후처리합니다.
- `ai` 모드는 패널별 문구를 프롬프트 마지막 `Text:` 블록으로 전달합니다.
- 모든 페이지가 완성된 뒤에만 `/api/comics/{id}/use`를 호출합니다. 중간 실패나 중지는 사용 처리하지 않습니다.
- 결과는 현재 저장 폴더의 `comic_maker/<시각>_<id>_<제목>/` 아래에 `comic_plan.json`과 `page_001.png` 형식으로 저장됩니다.
- NAIA 추가 편의기능 패널에서 **만화 만들기**를 누르면 활성화 토글 없이 바로 실행할 수 있습니다.
