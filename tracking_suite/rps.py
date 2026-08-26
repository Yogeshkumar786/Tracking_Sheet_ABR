"""
RPS report endpoint — inlined so the cloud job has no big dependencies.
Captured from rps_scraper_to_sheet.py. Only what routes.py needs.
"""
import json

RPS_REPORT_URL = ("http://smart.dsmsoft.com/FMSSmartApp/"
                  "Safex_RPS_Reports/WebService.asmx/getRpsReportData")
RPS_REPORT_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "http://smart.dsmsoft.com",
    "Referer": ("http://smart.dsmsoft.com/FMSSmartApp/"
                "Safex_RPS_Reports/RPS_Reports.aspx?usergroup=NRM.101"),
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/148.0.0.0 Safari/537.36"),
}
RPS_REQUEST_TIMEOUT = 60
RPS_BATCH_SIZE = 50

F_RPS = ("RPS_Number", "rpsNumber", "rps_number", "rpsNo", "RPS_No", "lrNumber", "tripId")
F_VEH = ("Vehicle_Number", "vehicleNumber", "vehicle_no", "vehicleNo", "VehicleNo", "VEHICLE_NUMBER")
F_ROUTE = ("Route", "routeName", "ROUTE_NAME", "route_name", "ROUTE")
F_START = ("Start_Time", "dispatchDate", "DISPATCH_DATE", "dispatch_date",
           "startDate", "START_TIME", "tripStartDate")
F_END = ("End_Time", "POD_DATE", "pod_date", "closureDate", "endDate",
         "END_TIME", "DELIVERY_DATE", "podDate")


def _parse_rps_response(body):
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    val = body.get("d")
    if val is None:
        return []
    if isinstance(val, list):
        return val
    s = str(val).strip()
    if s and s[0].isdigit():
        bracket = s.find("["); brace = s.find("{")
        start = bracket if (bracket != -1 and (brace == -1 or bracket < brace)) else brace
        if start != -1:
            s = s[start:]
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []
