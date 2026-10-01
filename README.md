# Home and Heal 메타 광고 리포트

메타 Marketing API로 Home and Heal 포트폴리오의 광고 계정 데이터를 매일 자동 수집하고, GitHub Pages 대시보드로 보여주는 저장소입니다.

- **자사몰 계정** (456254580008371): 픽셀 기준 일반 구매 지표
- **협력광고 계정** (네이버 브랜드스토어, 오늘의집, CJ온스타일): **공유 항목 포함** 구매·장바구니·전환값 (`catalog_segment_actions`, `catalog_segment_value`)

## 파일 구성

| 파일 | 역할 |
|---|---|
| `accounts.json` | 수집할 광고 계정 목록. `type`이 `own`이면 자사몰, `collab`이면 협력광고 |
| `scripts/fetch_meta.py` | 메타 API에서 일자 × 광고세트 단위 데이터를 받아 `data/`에 저장 |
| `.github/workflows/fetch.yml` | 매일 한국시간 오전 7시 자동 실행, 결과를 저장소에 커밋 |
| `index.html` | 대시보드 (기간 선택, 계정 탭, 직전 기간 대비 증감, CSV 다운로드) |
| `data/daily.csv`, `data/daily.json` | 광고세트 일별 성과 (첫 실행 후 자동 생성) |
| `data/ads.json`, `data/creatives.json`, `data/creatives/` | 소재(광고) 일별 성과, 소재 유형, 썸네일 |
| `data/bd_age.json` 외 3개 | 연령·성별·노출위치·기기별 캠페인 성과 |

## 세팅 순서

1. **저장소 만들기**: GitHub에서 새 저장소(예: `homeandheal-meta-report`)를 만들고 이 폴더의 파일을 전부 올립니다. `.github` 폴더도 꼭 함께 올려야 자동 실행이 됩니다.
2. **토큰 등록**: 저장소 → Settings → Secrets and variables → Actions → **New repository secret**
   - Name: `META_ACCESS_TOKEN`
   - Secret: 시스템 사용자 토큰 (`ads_read` 권한, 4개 계정 할당)
3. **(선택) API 버전**: 같은 화면의 Variables 탭에서 `META_API_VERSION`을 추가하면 버전을 바꿀 수 있습니다. 비워두면 `v26.0`.
4. **첫 수집 (과거 데이터 백필)**: Actions 탭 → "메타 데이터 수집" → **Run workflow** → 시작일·종료일 입력 (예: `2026-09-01` ~ `2026-09-30`).
   - 초록색 체크가 뜨면 `data/` 폴더가 생깁니다.
5. **대시보드 공개**: Settings → Pages → Source를 `Deploy from a branch`, Branch를 `main` / `(root)`로 저장합니다. 1~2분 뒤 `https://<깃허브아이디>.github.io/<저장소이름>/`에서 확인할 수 있습니다.

이후에는 매일 아침 7시에 최근 7일치를 다시 받아 갱신합니다. 전환은 기여 기간 동안 늦게 잡히므로 최근 일주일 값은 매일 업데이트됩니다.

## 계정 추가/변경

`accounts.json`에 줄을 추가하면 다음 실행부터 반영됩니다. 새 계정은 시스템 사용자에게도 자산 할당을 해줘야 합니다.

## 문제가 생기면

- Actions 실행 로그에서 `API 오류 (code=190)` → 토큰이 잘못됐거나 만료됨. 새 토큰으로 Secret 교체
- `code=200` / `code=10` → 해당 계정이 시스템 사용자에게 할당되지 않았거나 `ads_read` 권한이 없음
- 협력광고 계정의 구매값이 0 → 해당 기간 공유 항목 전환이 없거나, 계정 `type`이 `collab`으로 되어 있는지 확인

## 주의

- 저장소를 **공개(Public)**로 두면 GitHub Pages 대시보드와 `data/` 파일도 누구나 볼 수 있습니다. 광고주 데이터이므로 비공개 저장소를 권장합니다. (비공개 저장소의 Pages는 GitHub 유료 플랜이 필요합니다.)
- 토큰은 코드나 커밋에 절대 넣지 말고 Secrets에만 보관하세요.
