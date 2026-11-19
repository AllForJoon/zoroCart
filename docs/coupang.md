# 쿠팡 승인 환경 연동

쿠팡과 사전에 확인한 자동화 승인 범위의 문서 식별자와 해당 환경에서 검증한 DOM 정보를 로컬 JSON에 넣는다. 승인에 포함된 계정, 상품 검색·상세·장바구니 화면, 장바구니 추가 기능을 기준으로 범위를 작성한다. 전용 Chromium 프로필에서 사용자가 직접 로그인한 다음 그 브라우저 창을 닫는다. 쿠팡 이용약관은 다음에서 확인한다. [쿠팡 이용약관](https://m.coupang.com/nm/policies/terms)

```sh
python -m zorocart run --request '한국어 쇼핑 요청' \
  --coupang-approval private/approved-scope.json \
  --profile-dir private/browser \
  --output logs/coupang-run.json
```

승인 파일과 브라우저 프로필은 로컬에 보관한다. 브라우저 프로필에는 브라우저 세션이 들어갈 수 있다. 승인 파일의 `reference`에는 범위를 확인할 수 있는 문서 식별자를 넣는다.

## 승인 파일

| 키 | 값 |
| --- | --- |
| `reference` | 쿠팡의 사전 승인 또는 협의 기록의 식별자 |
| `home_url` | 승인된 쿠팡 HTTPS 상점 시작 URL |
| `cart_url` | 같은 오리진에서 승인된 장바구니 조회 URL |
| `read_paths` | 승인된 읽기 URL의 경로에 전체 일치시키는 정규식 배열. 검색어 등의 쿼리는 경로 비교에서 제외한다. |
| `add_path` | 상품과 판매 옵션을 장바구니에 추가하는 단일 POST 경로 |
| `add_fields` | `product`, `option`, `quantity` 세 키를 POST의 대응 필드 이름으로 매핑하는 객체. 평면 JSON과 URL 인코딩 폼을 지원한다. |
| `asset_hosts` | 필요한 경우에만 승인된 외부 HTTPS 이미지·스타일·스크립트·폰트 호스트를 넣는 배열 |
| `dom` | 승인된 페이지에서 검증한 CSS 셀렉터와 식별자 속성 매핑 |

`dom.views`는 `home`, `results`, `detail`, `cart` 화면을 식별한다. `blocked`를 추가하면 승인 환경의 접근 제한 화면을 더 명확히 식별할 수 있다. `dom.controls`에는 `search-input`, `search-submit`, `card`, `option`, `quantity`, `add`, `cart` 키를 넣는다. 상품과 장바구니 링크는 각각 해당 조회 URL에 대한 현재 탭 링크여야 한다.

`dom.fields`는 `name`, `price`, `pack`, `stock`, `shipping`, `shipping-price`, `attributes`, `quantity`, `option`, `category`를 대응하는 표시 요소에 연결한다. 중량·용량·인분 조건이 필요한 경우 `capacity`와 선택적으로 단위 속성 이름인 `unit_attr`를 채운다. 가격, 수량과 구성은 각각 해당 값을 나타내는 요소에 연결한다. 상세는 상품·옵션 컨테이너에서, 장바구니는 상품·옵션별 행에서 값을 읽는다. 현재 구현은 각 옵션의 사양과 가격이 상세 화면에 함께 표시되는 DOM을 지원한다.

`dom.product`, `dom.variant`, `dom.cart_row`, `dom.empty`, `dom.selected`는 각각 상품 컨테이너, 판매 옵션 컨테이너, 장바구니 상품 행, 빈 장바구니 표시, 선택된 옵션을 식별한다. `dom.key_attr`와 `dom.option_attr`는 화면 요소를 표시 상품 ID와 판매 옵션 ID에 연결하는 속성 이름이다. 검색 카드·상세 상품·장바구니 상품에서 같은 상품 ID를 읽고, 상세 옵션·장바구니 상품에서 같은 판매 옵션 ID를 읽을 수 있어야 한다.

조작 전 브라우저는 승인 범위, 현재 관찰 결과, 화면 요소, 옵션·수량·가격을 확인한다. 승인된 장바구니 추가 요청 본문의 상품·옵션·수량까지 일치할 때 한 번 전송한다. 로그인, CAPTCHA, 접근 제한, 화면 정보 부족, 추가 반영 여부가 불명확한 경우에는 확인한 상태를 남기고 중단한다. 쿠팡 DOM과 요청 필드는 승인된 환경에서 검증해야 한다.
