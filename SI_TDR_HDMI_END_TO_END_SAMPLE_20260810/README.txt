SI-TDR HDMI1 4채널 완전 관통 재현 입력
=======================================

이 폴더는 저장소나 별도 코드 폴더를 참조하지 않고 HDMI1의 CLK, DATA0,
DATA1, DATA2를 실행할 수 있는 독립 샘플입니다.

포함 범위:

  Channel / Batch       4 / 1
  Channel Path          8개
  요청 Port             16개
  예상 Touchstone       HDMI_2_0_IC5001_HDMI1_RX.s16p
  AEDT 기준             2024.2

Windows 경로 제한을 피하기 위해 ZIP을 C:\SI_TDR_HDMI처럼 짧은 경로에
푸는 것을 권장합니다.

Config 생성만 확인:

  python.exe .\runtime\main.py .\run.json --generate-config

전체 관통 실행:

  python.exe .\runtime\main.py .\run.json --generate-config --run-tdr --capture-pcb-routes

정상 완료 시 outputs 아래에 s16p, AEDT TDR 결과, PCB 이미지, 로그와 EDEN용
title.json, request.json, setting.json, result_detail.json, result.json이
생성됩니다. 고객 전달본의 03_HDMI_END_TO_END_SAMPLE에는 이 입력과 동일한
clean 실행 결과가 함께 제공됩니다.

CSV의 Net_Name은 참고 정보입니다. 자동화는 Designator와 Pin에서 실제
Channel Path를 찾습니다. BOM의 Part Number는 reference board Golden 재현값이므로
실제 제품 설정으로 사용하지 마십시오.

TDR 이미지의 Target Range:

  Config의 MinSpecOhm/MaxSpecOhm을 사용하며, 이 샘플은 90~110 ohm입니다.
  Ansys 원본 TDR 이미지에 하한선, 상한선과 Target Range 라벨이 표시됩니다.
  이 범위는 표시 기준이며 현재 버전에서는 Pass/Fail을 자동 판정하지 않습니다.
