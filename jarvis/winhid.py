"""Just enough of Windows' HID API (hid.dll + setupapi.dll through ctypes) to find a device and send or read
feature reports. No driver, admin rights or extra packages needed."""
from __future__ import annotations

import ctypes
import functools
from ctypes import wintypes

GENERIC_RW = 0x80000000 | 0x40000000
SHARE_RW = 0x1 | 0x2
OPEN_EXISTING = 3
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
HIDP_STATUS_SUCCESS = 0x00110000
INVALID_HANDLE = ctypes.c_void_p(-1).value


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


class InterfaceData(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("InterfaceClassGuid", GUID), ("Flags", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


class Attributes(ctypes.Structure):
    _fields_ = [("Size", ctypes.c_uint32), ("VendorID", ctypes.c_uint16), ("ProductID", ctypes.c_uint16),
                ("VersionNumber", ctypes.c_uint16)]


class Caps(ctypes.Structure):
    _fields_ = [("Usage", ctypes.c_uint16), ("UsagePage", ctypes.c_uint16),
                ("InputReportByteLength", ctypes.c_uint16), ("OutputReportByteLength", ctypes.c_uint16),
                ("FeatureReportByteLength", ctypes.c_uint16), ("Reserved", ctypes.c_uint16 * 17),
                ("rest", ctypes.c_uint16 * 10)]


@functools.lru_cache(maxsize=1)
def _libs():
    hid = ctypes.WinDLL("hid", use_last_error=True)
    setup, k32 = ctypes.WinDLL("setupapi"), ctypes.WinDLL("kernel32", use_last_error=True)
    setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setup.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setup.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                  wintypes.DWORD, ctypes.POINTER(InterfaceData)]
    setup.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(InterfaceData), ctypes.c_void_p,
                                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setup.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                wintypes.DWORD, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetAttributes.argtypes = [ctypes.c_void_p, ctypes.POINTER(Attributes)]
    hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(Caps)]
    hid.HidP_GetCaps.restype = ctypes.c_ulong
    hid.HidD_SetFeature.argtypes = hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG]
    for f in (hid.HidD_GetAttributes, hid.HidD_GetPreparsedData, hid.HidD_FreePreparsedData, hid.HidD_SetFeature,
              hid.HidD_GetFeature):
        f.restype = ctypes.c_ubyte     # BOOLEAN, not BOOL
    return hid, setup, k32


def _paths():
    hid, setup, _ = _libs()
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    devs = setup.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    try:
        i = 0
        while True:
            data = InterfaceData(cbSize=ctypes.sizeof(InterfaceData))
            if not setup.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), i, ctypes.byref(data)):
                return
            i += 1
            need = wintypes.DWORD()
            setup.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6   # cbSize
            if setup.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), buf, need, None, None):
                yield ctypes.wstring_at(ctypes.addressof(buf) + 4)
    finally:
        setup.SetupDiDestroyDeviceInfoList(devs)


class Device:
    def __init__(self, handle, product: int, feature_len: int, path: str):
        self.h, self.product, self.feature_len, self.path = handle, product, feature_len, path
        self.hid = _libs()[0]

    def send(self, report: bytes):
        buf = ctypes.create_string_buffer(bytes(report[:self.feature_len]).ljust(self.feature_len, b"\0"),
                                          self.feature_len)
        if not self.hid.HidD_SetFeature(self.h, buf, self.feature_len):
            raise OSError(f"the lighting chip refused a command (error {ctypes.get_last_error()})")

    def read(self, report_id: int) -> bytes:
        buf = ctypes.create_string_buffer(self.feature_len)
        buf[0] = report_id
        if not self.hid.HidD_GetFeature(self.h, buf, self.feature_len):
            raise OSError(f"the lighting chip didn't answer (error {ctypes.get_last_error()})")
        return buf.raw

    def close(self):
        if self.h:
            _libs()[2].CloseHandle(self.h)
            self.h = None


def open_first(vendors, products, usage_page: int, usage: int) -> Device | None:
    hid, _, k32 = _libs()
    for path in _paths():
        low = path.lower()
        if not any(f"vid_{v:04x}" in low for v in vendors):
            continue
        h = k32.CreateFileW(path, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h in (None, INVALID_HANDLE):
            h = k32.CreateFileW(path, 0, SHARE_RW, None, OPEN_EXISTING, 0, None)
            if h in (None, INVALID_HANDLE):
                continue
        attrs, caps, pp = Attributes(Size=ctypes.sizeof(Attributes)), Caps(), ctypes.c_void_p()
        ok = (hid.HidD_GetAttributes(h, ctypes.byref(attrs)) and attrs.VendorID in vendors
              and attrs.ProductID in products and hid.HidD_GetPreparsedData(h, ctypes.byref(pp)))
        if ok:
            try:
                ok = hid.HidP_GetCaps(pp, ctypes.byref(caps)) == HIDP_STATUS_SUCCESS
            finally:
                hid.HidD_FreePreparsedData(pp)
        if ok and caps.UsagePage == usage_page and caps.Usage == usage and caps.FeatureReportByteLength:
            return Device(h, attrs.ProductID, caps.FeatureReportByteLength, path)
        k32.CloseHandle(h)
    return None
