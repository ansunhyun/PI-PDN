# InnerCap 및 PDN 설정 규칙

## InnerCap

- 복수 핀은 쉼표로 구분한다. 핀 개수와 quantity가 같으면 각 핀에 1개를 생성한다.
- 단일 핀은 quantity개를 생성한다. 복수 핀과 수량이 다르면 중단한다.
- quantity=0은 제외한다. 음수, 소수 수량과 중복 핀은 오류다.
- 필수 열은 Designator, Pin_number, quantity, Cap., Part_number다. 수량이 양수인 행은 다섯 값 모두 필요하다. quantity의 빈 값은 오류이며 기본 수량을 추정하지 않는다. quantity=0인 행은 다른 값이 비어 있어도 제외한다.
- Designator는 실제 PCB IC 참조명이다. SoC나 Spec으로 다른 IC를 추정하지 않는다.
- PCB net name, SoC, Vendor, Voltage, Tolerance, Decap은 선택적인 참고 정보로 보존한다. 빈 값이어도 허용하며 IC와 핀을 선택하거나 변경하는 조건으로 사용하지 않는다.
- Cap.는 전기적 용량, Part_number는 부품 식별에 사용한다. 둘 중 하나라도 없으면 파싱 단계에서 중단한다. 임의 용량이나 부품번호를 추정하지 않는다.
- 100nF_LSC는 100nF로 정규화한다. Part_number는 유지한다.
- IC, 핀, GND 또는 커패시터 생성 실패는 로그를 남기고 실행을 중단한다.

## PDN setup

- 프로파일의 .sws와 .sfsdf를 모두 PDN_setup 검색 경로에서 찾는다.
- .sws 옵션을 먼저 적용한 뒤 .sfsdf의 주파수 구간을 적용한다.
- SIwave 2025 R2 Scripting Guide의 ScrClearAllSweeps("syz") 및 ScrAppendSweep("syz", start_hz, stop_hz, count, is_log)를 사용한다.
- DEC는 ByDecade, LINC는 Linear 분포다. Hz/kHz/MHz/GHz를 Hz로 변환한다.
- 파일 누락, 잘못된 구간 또는 COM 반환값 실패 시 해석을 진행하지 않는다.
- 전체 실행 후 solver의 .cfg 주파수 목록이 목표 범위와 일치하는지 확인한다. API 호출 검증만으로 실제 해석 완료를 보장하지 않는다.

## 검증 범위

OLED77T6 입력은 29개 핀별 커패시터 항목으로 파싱된다. FR4_4L_Freq.sfsdf는 100 kHz부터 1 GHz까지 5개 구간이다. 실제 AEDB 커패시터 생성과 SIwave 해석은 Ansys 실행 환경에서 별도로 검증해야 한다.
