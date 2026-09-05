# src/monitor/uds_monitor.py
import time
import os
import can
import isotp
import yaml
from ..logger.base_logger import log_event

# 모니터링 설정값

CAN_CHANNEL = "can0"
TARGET_UDS_ID = 0x6A6
UDS_RESPONSE_OFFSET = 0x6A
PADDING_BYTE = 0xAA

# ISO-TP 기본 설정
isotp_params = {
    "stmin": 0,
    "blocksize": 8,
    "wftmax": 0,
    "tx_data_length": 8,
    "tx_padding": PADDING_BYTE,
    "rx_flowcontrol_timeout": 1000,
    "rx_consecutive_frame_timeout": 1000,
}

# 스코어 설정
SCORE_FAIL = 1.0               
SCORE_SUCCESS = 0.0            

# DTC 결정 상수
DTC_Threshold = 10

# NRC config 로드

def load_nrc_fail_list(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"NRC config not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict) or "nrc_fail_list" not in data:
        raise ValueError("Invalid NRC config: missing 'nrc_fail_list'")

    fail_set = set()
    for item in data["nrc_fail_list"]:
        try:
            nrc = int(item, 16) if isinstance(item, str) else int(item)
            fail_set.add(nrc)
        except Exception as e:
            raise ValueError(f"Invalid NRC value {item} ({e})")

    return fail_set



# UDS 모니터 클래스

class UDSMonitor:
    def __init__(self, nrc_cfg_path: str = "config/nrc_fail_list.yaml"):
        self.NRC_FAIL_SET = load_nrc_fail_list(nrc_cfg_path)

        self.bus = can.interface.Bus(channel=CAN_CHANNEL, bustype="socketcan")
        addr = isotp.Address(
            isotp.AddressingMode.Normal_11bits,
            txid=TARGET_UDS_ID,
            rxid=TARGET_UDS_ID + UDS_RESPONSE_OFFSET
        )
        self.stack = isotp.CanStack(bus=self.bus, address=addr, params=isotp_params)

        self._fail_score = 0.0
        self.events = []

    def _emit(self, metric, value, status="FAIL"):
        self.events.append({
            "type": "uds",
            "id": TARGET_UDS_ID,
            "metric": metric,
            "value": value,
            "status": status,
            "ts_ms": int(time.time() * 1000),
        })


    # ISO-TP 송신

    def send_request(self, data):
        self.stack.send(bytes(data))
        while self.stack.transmitting():
            self.stack.process()
            time.sleep(0.01)

    # ISO-TP 수신

    def recv_response(self, timeout=1.0):
        start = time.time()
        while time.time() - start < timeout:
            self.stack.process()
            if self.stack.available():
                resp = list(self.stack.recv())
                if len(resp) >= 3 and resp[0] == 0x7F and resp[2] == 0x78:
                    log_event("uds", TARGET_UDS_ID, "NRC_0x78_pending", "wait_more", "INFO")
                    continue 
                return resp
            time.sleep(0.01)
        return None



    def start(self) -> float:
        """
        UDS 모니터링 시작
        
        :return: 최종 FAIL 스코어 (0.0 = 모두 성공, 1.0 = 하나라도 실패)
        """
        self._fail_score = 0.0  # 스코어 초기화
        
        try:
            # 0x10 세션 진입
            print("[INFO] UDS Monitor - Checking session entry (0x10)...")
            if not self._send_once_or_retry([0x10, 0x02], "session_entry"):
                print("[FAIL] UDS Monitor - Session entry failed")
                log_event("uds", TARGET_UDS_ID, "monitor_result", "session_entry_fail", "FAIL")
                self._fail_score = SCORE_FAIL
                self._emit("session_entry", "no_valid_response")
                return self._fail_score

            # 0x3E Tester Present
            print("[INFO] UDS Monitor - Checking tester present (0x3E)...")
            if not self._send_once_or_retry([0x3E, 0x00], "tester_present"):
                print("[FAIL] UDS Monitor - Tester present failed")
                log_event("uds", TARGET_UDS_ID, "monitor_result", "tester_present_fail", "FAIL")
                self._fail_score = SCORE_FAIL
                self._emit("tester_present", "no_valid_response")
                return self._fail_score

            # 0x19 DTC 읽기
            print("[INFO] UDS Monitor - Reading DTC (0x19)...")
            self.send_request([0x19, 0x02])
            total_dtc, dtc_failed = self.collect_all_dtc()
            
          
            if dtc_failed:
                print(f"[FAIL] UDS Monitor - DTC collection failed")
                log_event("uds", TARGET_UDS_ID, "monitor_result", "dtc_collection_fail", "FAIL")
                self._fail_score = SCORE_FAIL
                self._emit("dtc_collection", "failed")
                return self._fail_score
            
            # DTC 개수 기준 초과
            if total_dtc > DTC_Threshold:
                print(f"[FAIL] UDS Monitor - DTC found: {total_dtc} DTC(s)")
                log_event("uds", TARGET_UDS_ID, "monitor_result", f"dtc_found_{total_dtc}", "FAIL")
                self._fail_score = SCORE_FAIL
                self._emit("dtc_count", total_dtc)
                return self._fail_score
            
            # 모든 검사 통과
            print(f"[OK] UDS Monitor - All checks passed (DTC count: {total_dtc})")
            log_event("uds", TARGET_UDS_ID, "monitor_result", "all_passed", "OK")
            self._fail_score = SCORE_SUCCESS

        except Exception as e:
            print(f"[FAIL] UDS Monitor - Exception occurred: {e}")
            log_event("uds", TARGET_UDS_ID, "monitor_result", f"exception_{str(e)}", "FAIL")
            self._fail_score = SCORE_FAIL
            self._emit("exception", str(e))
        
        print(f"[INFO] UDS Monitor finished - Final FAIL score: {self._fail_score:.1f}")
        return self._fail_score



    def _send_once_or_retry(self, data, step_name):
        """
        요청 보내고 응답 확인, 없으면 한 번만 재시도
        
        :param data: 전송할 UDS 요청 데이터
        :param step_name: 단계 이름 (로깅용)
        :return: 성공 여부 (True/False)
        """
    
        self.send_request(data)
        resp = self.recv_response(timeout=1.0)

        if not resp:
        
            print(f"[WARN] {step_name} - No response, retrying...")
            self.send_request(data)
            resp = self.recv_response(timeout=1.0)

            if not resp:
                print(f"[FAIL] {step_name} - No response after retry")
                log_event("uds", TARGET_UDS_ID, f"{step_name}_no_response", "fail", "FAIL")
                return False

        # NRC 응답
        if resp[0] == 0x7F:
            nrc = resp[2]
            print(f"[NRC] {step_name} - Received NRC: {hex(nrc)}")
            
            if nrc in self.NRC_FAIL_SET:
                print(f"[FAIL] {step_name} - NRC {hex(nrc)} is in fail list")
                log_event("uds", TARGET_UDS_ID, f"{step_name}_NRC_{hex(nrc)}", "fail_list_hit", "FAIL")
                return False
            else:
           
                print(f"[WARN] {step_name} - NRC {hex(nrc)} not in fail list (continuing)")
                log_event("uds", TARGET_UDS_ID, f"{step_name}_NRC_unknown_{hex(nrc)}", nrc, "WARN")
                return True

        # 정상 응답
        print(f"[OK] {step_name} - Response OK")
        log_event("uds", TARGET_UDS_ID, step_name, "response_ok", "OK")
        return True

 

    # 0x59 0x02 DTC 개수 계산
    def collect_all_dtc(self):
        """
        DTC 데이터 수집
        
        :return: (total_dtc_count, failed)
                 - total_dtc_count: 총 DTC 개수
                 - failed: NRC 실패 여부
        """
        accumulated_data = bytearray()
        start_time = time.time()
        failed = False

        while time.time() - start_time < 2.0:
            resp = self.recv_response(timeout=0.5)
            if not resp:
                break

            # 정상 DTC 응답
            if len(resp) >= 4 and resp[0] == 0x59 and resp[1] == 0x02:
                # status mask(resp[2]) 건너뛰고 resp[3:]부터 누적
                accumulated_data.extend(resp[3:])
                print(f"[DTC] Received {len(resp[3:])} bytes of DTC data")

            # NRC 응답
            elif resp[0] == 0x7F:
                nrc = resp[2]
                print(f"[NRC] DTC read - Received NRC: {hex(nrc)}")
                
                if nrc in self.NRC_FAIL_SET:
                    print(f"[FAIL] DTC - NRC {hex(nrc)} is in fail list")
                    log_event("uds", TARGET_UDS_ID, f"DTC_NRC_{hex(nrc)}", "fail_list_hit", "FAIL")
                    failed = True
                    break  # 실패 즉시 중단
                else:
                    print(f"[WARN] DTC - NRC {hex(nrc)} not in fail list (continuing)")
                    log_event("uds", TARGET_UDS_ID, f"DTC_NRC_unknown_{hex(nrc)}", nrc, "WARN")
                    continue

        total_dtc = len(accumulated_data) // 4  # 3바이트 코드 + 1바이트 상태
        print(f"[DTC] Total DTC count: {total_dtc}")
        log_event("uds", TARGET_UDS_ID, "DTC_count", total_dtc, "OK" if total_dtc == 0 else "INFO")
        
        return total_dtc, failed
    
    
    def get_fail_score(self) -> float:
        """
        현재까지 누적된 FAIL 스코어 반환
        0.0 = 모두 성공, 1.0 = 하나라도 실패
        """
        return self._fail_score

    def fetch_events(self):
        events = self.events[:]
        self.events.clear()
        return events
