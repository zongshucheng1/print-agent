"""Print immutable PNG labels with a per-job Windows GDI device context.

No default printer, driver setting, form, or queue is changed. Importing this
module does not load pywin32 or touch the spooler. A spool submission is never
reported as proof that a physical label has left the printer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
from ctypes import wintypes
from math import isfinite
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class PrintSettings:
    printer_name: str
    paper_width_mm: float = 80.0
    paper_height_mm: float = 80.0
    copies: int = 1
    orientation: str = "portrait"
    offset_x_mm: float = 0.0
    offset_y_mm: float = 0.0
    dpi: int | None = None
    document_name: str = "Deqin inventory label"

    def validate(self) -> None:
        if not isinstance(self.printer_name, str) or not self.printer_name.strip() or "\x00" in self.printer_name:
            raise ValueError("A specific Windows printer queue is required")
        for value in (self.paper_width_mm, self.paper_height_mm, self.offset_x_mm, self.offset_y_mm):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
                raise ValueError("Paper dimensions and offsets must be finite numbers")
        if not (1 <= self.paper_width_mm <= 300 and 1 <= self.paper_height_mm <= 1000):
            raise ValueError("Paper dimensions are outside the supported label range")
        if abs(self.offset_x_mm) > 25 or abs(self.offset_y_mm) > 25:
            raise ValueError("Print offsets must be between -25 and 25 mm")
        if type(self.copies) is not int or not 1 <= self.copies <= 100:
            raise ValueError("Copies must be an integer from 1 to 100")
        if self.orientation not in ("portrait", "landscape"):
            raise ValueError("Orientation must be portrait or landscape")
        if self.dpi is not None and (type(self.dpi) is not int or not 72 <= self.dpi <= 2400):
            raise ValueError("Requested DPI must be an integer from 72 to 2400")
        if not isinstance(self.document_name, str) or not self.document_name.strip() or len(self.document_name) > 160 or "\x00" in self.document_name:
            raise ValueError("Document name must contain 1 to 160 characters")


@dataclass(frozen=True)
class PrintSubmission:
    job_id: int
    printer_name: str
    document_name: str
    copies: int
    dpi_x: int
    dpi_y: int
    paper_width_mm: float
    paper_height_mm: float
    status: str = "SUBMITTED"
    delivery_kind: str = "WINDOWS_SPOOL"
    physical_completion_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PrintAdapterError(RuntimeError):
    def __init__(self, code: str, message: str, *, phase: str = "BEFORE_SEND", job_id: int | None = None):
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.job_id = job_id


def configure_devmode(devmode: Any, settings: PrintSettings, constants: Any) -> Any:
    """Change only standard per-job fields on the driver's own DEVMODE.

    DriverExtra bytes (thermal transfer mode, ribbon/media sensing, darkness,
    and other manufacturer settings) are retained. Do not create a blank
    DEVMODE or call SetPrinter/SetDefaultPrinter/AddForm.
    """
    settings.validate()
    # Width/height describe the final oriented label, not unrotated stock.
    width, height = settings.paper_width_mm, settings.paper_height_mm
    if settings.orientation == "landscape":
        width, height = height, width
    # Windows specifies PaperSize=0 when dimensions describe custom stock.
    # A stale named form (e.g. A4) must not override these per-job dimensions.
    devmode.PaperSize = 0
    devmode.Fields &= ~constants.DM_FORMNAME
    devmode.PaperWidth = round(width * 10)
    devmode.PaperLength = round(height * 10)
    devmode.Orientation = constants.DMORIENT_LANDSCAPE if settings.orientation == "landscape" else constants.DMORIENT_PORTRAIT
    # Emit one page per requested copy; never multiply by driver Copies too.
    devmode.Copies = 1
    devmode.Scale = 100
    devmode.Fields |= constants.DM_PAPERSIZE | constants.DM_PAPERWIDTH | constants.DM_PAPERLENGTH | constants.DM_ORIENTATION | constants.DM_COPIES | constants.DM_SCALE
    if settings.dpi is not None:
        devmode.PrintQuality = settings.dpi
        devmode.YResolution = settings.dpi
        devmode.Fields |= constants.DM_PRINTQUALITY | constants.DM_YRESOLUTION
    return devmode


def _geometry(dc: Any) -> dict[str, int | float]:
    # GetDeviceCaps values are stable Win32 constants, independent of pywin32.
    values = {"dpi_x": dc.GetDeviceCaps(88), "dpi_y": dc.GetDeviceCaps(90),
              "printable_width_px": dc.GetDeviceCaps(8), "printable_height_px": dc.GetDeviceCaps(10),
              "physical_width_px": dc.GetDeviceCaps(110), "physical_height_px": dc.GetDeviceCaps(111),
              "offset_x_px": dc.GetDeviceCaps(112), "offset_y_px": dc.GetDeviceCaps(113)}
    if any(int(values[k]) <= 0 for k in ("dpi_x", "dpi_y", "printable_width_px", "printable_height_px", "physical_width_px", "physical_height_px")):
        raise PrintAdapterError("INVALID_DRIVER_GEOMETRY", "Printer driver returned invalid paper geometry")
    if any(int(values[k]) < 0 for k in ("offset_x_px", "offset_y_px")):
        raise PrintAdapterError("INVALID_DRIVER_GEOMETRY", "Printer driver returned negative hardware margins")
    values["paper_width_mm"] = values["physical_width_px"] * 25.4 / values["dpi_x"]
    values["paper_height_mm"] = values["physical_height_px"] * 25.4 / values["dpi_y"]
    return values


class _DOCINFOW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_int), ("lpszDocName", ctypes.c_wchar_p),
                ("lpszOutput", ctypes.c_wchar_p), ("lpszDatatype", ctypes.c_wchar_p),
                ("fwType", ctypes.c_uint32)]


class _GdiPrintApi:
    """The native GDI lifecycle retains StartDocW's actual spool identifier.

    PyCDC.StartDoc discards its integer result and returns None. It cannot be
    used for an audited job ID. All calls here use the same printer HDC; they
    do not use StartDocPrinter/RAW or create a second job.
    """
    def __init__(self, library: Any | None = None) -> None:
        self.library = library if library is not None else ctypes.WinDLL("gdi32", use_last_error=True)
        self.library.StartDocW.argtypes = [wintypes.HDC, ctypes.POINTER(_DOCINFOW)]
        self.library.StartDocW.restype = ctypes.c_int
        for name in ("StartPage", "EndPage", "EndDoc", "AbortDoc"):
            function = getattr(self.library, name)
            function.argtypes = [wintypes.HDC]
            function.restype = ctypes.c_int

    @staticmethod
    def _require_success(value: int, operation: str) -> int:
        if value <= 0:
            # Error text deliberately excludes document names and driver data.
            code = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            raise OSError(code, f"GDI {operation} failed")
        return value

    def start_doc(self, hdc: int, document_name: str) -> int:
        info = _DOCINFOW(ctypes.sizeof(_DOCINFOW), document_name, None, None, 0)
        return self._require_success(self.library.StartDocW(hdc, ctypes.byref(info)), "StartDocW")

    def lifecycle(self, hdc: int, operation: str) -> None:
        self._require_success(getattr(self.library, operation)(hdc), operation)


class _GdiDeviceContext:
    def __init__(self, dc: Any, api: _GdiPrintApi):
        self.dc, self.api = dc, api
        self.hdc = dc.GetHandleOutput()

    def GetDeviceCaps(self, value): return self.dc.GetDeviceCaps(value)
    def GetHandleOutput(self): return self.hdc
    def DeleteDC(self): return self.dc.DeleteDC()
    def StartDoc(self, name): return self.api.start_doc(self.hdc, name)
    def StartPage(self): self.api.lifecycle(self.hdc, "StartPage")
    def EndPage(self): self.api.lifecycle(self.hdc, "EndPage")
    def EndDoc(self): self.api.lifecycle(self.hdc, "EndDoc")
    def AbortDoc(self): self.api.lifecycle(self.hdc, "AbortDoc")


class _PyWin32Backend:
    def __init__(self) -> None:
        try:
            import win32con
            import win32gui
            import win32print
            import win32ui
        except ImportError as error:
            raise PrintAdapterError("PYWIN32_UNAVAILABLE", "Windows printing requires pywin32 in the agent environment") from error
        self.con, self.gui, self.print, self.ui = win32con, win32gui, win32print, win32ui
        self.gdi = _GdiPrintApi()

    def list_printers(self) -> list[dict[str, Any]]:
        rows = self.print.EnumPrinters(self.print.PRINTER_ENUM_LOCAL | self.print.PRINTER_ENUM_CONNECTIONS, None, 2)
        return [{"name": r.get("pPrinterName"), "driver": r.get("pDriverName"), "port": r.get("pPortName"),
                 "port_target": self.port_target(r.get("pPortName")),
                 "status_flags": int(r.get("Status", 0)), "queued_jobs": int(r.get("cJobs", 0))} for r in rows]

    @staticmethod
    def port_target(port: str) -> dict[str, Any]:
        """Read only public endpoint identity, never SNMP communities or credentials."""
        if not isinstance(port, str) or not port or any(char in port for char in "\\/,\x00"):
            return {"kind": "unverified"}
        try:
            import winreg
            path = "SYSTEM\\CurrentControlSet\\Control\\Print\\Monitors\\Standard TCP/IP Port\\Ports\\" + port
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ) as key:
                values = {}
                for source, target in (("HostName", "host"), ("IPAddress", "address"),
                                       ("Protocol", "protocol"), ("PortNumber", "port"), ("Queue", "queue")):
                    try:
                        value = winreg.QueryValueEx(key, source)[0]
                    except OSError:
                        continue
                    if isinstance(value, (str, int)):
                        values[target] = value
                return {"kind": "standard-tcpip", **values} if values else {"kind": "unverified"}
        except (ImportError, OSError):
            return {"kind": "unverified"}

    def printer_info(self, name: str) -> dict[str, Any]:
        handle = self.print.OpenPrinter(name)
        try:
            row = self.print.GetPrinter(handle, 2)
            mode = row.get("pDevMode")
            return {"name": row.get("pPrinterName", name), "driver": row.get("pDriverName"),
                    "port": row.get("pPortName"), "status_flags": int(row.get("Status", 0)),
                    "port_target": self.port_target(row.get("pPortName")),
                    "queued_jobs": int(row.get("cJobs", 0)),
                    "configured_paper_width_mm": getattr(mode, "PaperWidth", 0) / 10 if mode else None,
                    "configured_paper_height_mm": getattr(mode, "PaperLength", 0) / 10 if mode else None,
                    "configured_orientation": getattr(mode, "Orientation", None),
                    "configured_dpi_x": getattr(mode, "PrintQuality", None),
                    "configured_dpi_y": getattr(mode, "YResolution", None),
                    "media_mode": "DRIVER_PRIVATE_SETTINGS_PRESERVED"}
        finally:
            self.print.ClosePrinter(handle)

    def prepare_dc(self, settings: PrintSettings) -> Any:
        handle = self.print.OpenPrinter(settings.printer_name)
        try:
            info = self.print.GetPrinter(handle, 2)
            mode = info.get("pDevMode")
            if mode is None:
                raise PrintAdapterError("DRIVER_DEVMODE_UNAVAILABLE", "Printer driver did not provide its DEVMODE")
            configure_devmode(mode, settings, self.con)
            result = self.print.DocumentProperties(0, handle, settings.printer_name, mode, mode,
                                                   self.con.DM_IN_BUFFER | self.con.DM_OUT_BUFFER)
            if result != 1:
                raise PrintAdapterError("DRIVER_SETTINGS_REJECTED", "Printer driver rejected the requested per-job settings")
            if int(mode.Copies) != 1:
                raise PrintAdapterError("DRIVER_COPIES_MISMATCH", "Printer driver did not accept one copy per page")
            if mode.Fields & self.con.DM_SCALE and int(mode.Scale) != 100:
                raise PrintAdapterError("DRIVER_SCALE_MISMATCH", "Printer driver did not accept actual-size printing")
            hdc = self.gui.CreateDC("WINSPOOL", settings.printer_name, mode)
            try:
                dc = self.ui.CreateDCFromHandle(hdc)
            except Exception:
                self.gui.DeleteDC(hdc)
                raise
            return _GdiDeviceContext(dc, self.gdi)
        finally:
            self.print.ClosePrinter(handle)

    def draw_image(self, dc: Any, image: Any, rectangle: tuple[int, int, int, int]) -> None:
        from PIL import ImageWin
        ImageWin.Dib(image).draw(dc.GetHandleOutput(), rectangle)

    def query_job(self, name: str, job_id: int) -> dict[str, Any] | None:
        handle = self.print.OpenPrinter(name)
        try:
            try:
                return self.print.GetJob(handle, job_id, 2)
            except Exception as error:
                # Only known "not found" codes mean absent. Permissions/network
                # failures remain errors and never become an absent/printed job.
                code = getattr(error, "winerror", None)
                if code is None and getattr(error, "args", None):
                    code = error.args[0]
                if code in (2, 87, 1168):
                    return None
                raise
        finally:
            self.print.ClosePrinter(handle)


class WindowsPrinterAdapter:
    def __init__(self, backend: Any | None = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> Any:
        if self._backend is None:
            self._backend = _PyWin32Backend()
        return self._backend

    def list_printers(self) -> list[dict[str, Any]]:
        try:
            return self.backend.list_printers()
        except PrintAdapterError:
            raise
        except Exception as error:
            raise PrintAdapterError("PRINTER_ENUMERATION_FAILED", "Unable to read Windows printer queues") from error

    def diagnostics(self, printer_name: str, settings: PrintSettings | None = None) -> dict[str, Any]:
        """Read driver information; optional per-job geometry probe never starts a job."""
        dc = None
        try:
            result = self.backend.printer_info(printer_name)
            if settings is not None:
                settings.validate()
                if settings.printer_name != printer_name:
                    raise ValueError("Diagnostic queue and print settings disagree")
                dc = self.backend.prepare_dc(settings)
                result["geometry"] = _geometry(dc)
                self._check_geometry(settings, result["geometry"])
            result["physical_completion_supported"] = False
            return result
        except PrintAdapterError:
            raise
        except Exception as error:
            raise PrintAdapterError("PRINTER_DIAGNOSTIC_FAILED", "Unable to inspect the selected Windows printer") from error
        finally:
            if dc is not None:
                try:
                    dc.DeleteDC()
                except Exception:
                    pass

    @staticmethod
    def _check_geometry(settings: PrintSettings, geometry: dict[str, Any]) -> None:
        # Permit integer device-dot rounding, not silent fallback to A4/4x6.
        for dimension, dpi in (("width", "x"), ("height", "y")):
            tolerance = max(0.25, 2 * 25.4 / geometry[f"dpi_{dpi}"])
            if abs(geometry[f"paper_{dimension}_mm"] - getattr(settings, f"paper_{dimension}_mm")) > tolerance:
                raise PrintAdapterError("PAPER_SIZE_MISMATCH", "Driver paper size differs from the frozen label size; configure the queue and retry")
        if settings.dpi is not None and (geometry["dpi_x"] != settings.dpi or geometry["dpi_y"] != settings.dpi):
            raise PrintAdapterError("DPI_MISMATCH", "Printer driver did not accept the frozen DPI")

    def submit_png(self, png_path: str | Path, settings: PrintSettings, *, before_submit: Callable[[], None],
                   on_job_created: Callable[[int], None] | None = None,
                   on_submitted: Callable[[PrintSubmission], None] | None = None) -> PrintSubmission:
        """Submit once. before_submit MUST durably authorize/record sending.

        If that callback fails, StartDoc is never called. Once StartDoc is
        attempted, every exception is MAY_HAVE_SENT, including AbortDoc errors.
        on_job_created lets the durable ledger retain the spool ID immediately.
        """
        dc, image, job_id, phase, document_started = None, None, None, "BEFORE_SEND", False
        try:
            settings.validate()
            if not callable(before_submit):
                raise ValueError("A durable before_submit callback is required")
            path = Path(png_path)
            if not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024 * 1024:
                raise PrintAdapterError("INVALID_LABEL_FILE", "Label PNG must be a nonempty file of at most 16 MiB")
            from PIL import Image, ImageChops
            with Image.open(path) as source:
                if source.format != "PNG" or getattr(source, "n_frames", 1) != 1 or source.width * source.height > 16_000_000:
                    raise PrintAdapterError("INVALID_LABEL_IMAGE", "A single-frame PNG with at most 16 million pixels is required")
                source.load()
                expected_ratio = settings.paper_width_mm / settings.paper_height_mm
                if abs(source.width / source.height / expected_ratio - 1) > 0.01:
                    raise PrintAdapterError("LABEL_ASPECT_MISMATCH", "Frozen PNG aspect ratio differs from the configured label")
                with source.convert("RGBA") as rgba:
                    image = Image.new("RGB", rgba.size, "white")
                    with rgba.getchannel("A") as alpha:
                        image.paste(rgba, mask=alpha)
            dc = self.backend.prepare_dc(settings)
            geometry = _geometry(dc)
            self._check_geometry(settings, geometry)
            width = round(settings.paper_width_mm * geometry["dpi_x"] / 25.4)
            height = round(settings.paper_height_mm * geometry["dpi_y"] / 25.4)
            if width * height > 32_000_000:
                raise PrintAdapterError("RENDER_SIZE_EXCEEDED", "Driver DPI would produce an oversized bitmap")
            rendered = image.resize((width, height), Image.Resampling.NEAREST)
            image.close()
            image = rendered
            x = round(settings.offset_x_mm * geometry["dpi_x"] / 25.4) - geometry["offset_x_px"]
            y = round(settings.offset_y_mm * geometry["dpi_y"] / 25.4) - geometry["offset_y_px"]
            # Allow the physical page's white margin outside the printable area,
            # but fail before sending if actual content would be clipped.
            with Image.new("RGB", image.size, "white") as background:
                with ImageChops.difference(image, background) as difference:
                    content = difference.getbbox()
            if content and (content[0] + x < 0 or content[1] + y < 0 or content[2] + x > geometry["printable_width_px"] or content[3] + y > geometry["printable_height_px"]):
                raise PrintAdapterError("CONTENT_OUTSIDE_PRINTABLE_AREA", "Label content exceeds printable margins; fix the layout or calibrated offsets")
            before_submit()
            phase = "MAY_HAVE_SENT"
            # A StartDoc call may create a spool job before failing/returning
            # an invalid value. Cleanup is attempted even without a known ID.
            document_started = True
            job_id = dc.StartDoc(settings.document_name)
            if type(job_id) is not int or job_id <= 0:
                job_id = None
                raise PrintAdapterError("INVALID_SPOOL_JOB_ID", "Windows did not return a valid spool job ID", phase=phase)
            if on_job_created is not None:
                on_job_created(job_id)
            for _ in range(settings.copies):
                dc.StartPage()
                self.backend.draw_image(dc, image, (x, y, x + width, y + height))
                dc.EndPage()
            dc.EndDoc()
            document_started = False
            submission = PrintSubmission(job_id, settings.printer_name, settings.document_name, settings.copies,
                                   geometry["dpi_x"], geometry["dpi_y"], settings.paper_width_mm, settings.paper_height_mm)
            if on_submitted is not None:
                on_submitted(submission)
            return submission
        except Exception as error:
            if dc is not None and document_started:
                try:
                    dc.AbortDoc()
                except Exception:
                    pass
            if isinstance(error, PrintAdapterError):
                raise PrintAdapterError(error.code, str(error), phase=phase, job_id=job_id) from error
            code = "PRINT_PREPARATION_FAILED" if phase == "BEFORE_SEND" else "PRINT_SUBMISSION_UNCERTAIN"
            raise PrintAdapterError(code, "Printing stopped before submission" if phase == "BEFORE_SEND" else "Print submission is uncertain; do not automatically resend", phase=phase, job_id=job_id) from error
        finally:
            if image is not None:
                image.close()
            if dc is not None:
                try:
                    dc.DeleteDC()
                except Exception:
                    # Cleanup cannot replace the recorded submission outcome.
                    pass

    def query_job(self, printer_name: str, job_id: int, *, expected_document_name: str | None = None) -> dict[str, Any]:
        if type(job_id) is not int or job_id <= 0:
            raise PrintAdapterError("INVALID_JOB_ID", "Spool job ID must be a positive integer")
        try:
            row = self.backend.query_job(printer_name, job_id)
        except PrintAdapterError:
            raise
        except Exception as error:
            raise PrintAdapterError("JOB_QUERY_FAILED", "Unable to query the Windows spool job; do not resend", phase="MAY_HAVE_SENT", job_id=job_id) from error
        base = {"job_id": job_id, "printer_name": printer_name, "physical_completion_confirmed": False}
        if row is None:
            return {**base, "state": "NOT_FOUND", "message": "Spool job is absent; absence does not prove printing or non-printing"}
        if expected_document_name is not None and row.get("pDocument") != expected_document_name:
            return {**base, "state": "IDENTITY_MISMATCH", "message": "Spool job identity changed; manual reconciliation required"}
        flags = int(row.get("Status", 0))
        if flags & (0x100 | 0x4):
            state = "DELETED"
        elif flags & (0x2 | 0x20 | 0x40 | 0x200 | 0x400):
            state = "ATTENTION_REQUIRED"
        elif flags & 0x80:
            state = "SPOOLER_REPORTED_PRINTED"
        elif flags & 0x1000:
            state = "SENT_TO_PRINTER"
        elif flags & 0x1:
            state = "PAUSED"
        elif flags & 0x10:
            state = "PRINTING"
        elif flags & 0x8:
            state = "SPOOLING"
        else:
            state = "QUEUED"
        return {**base, "state": state, "status_flags": flags, "document_name": row.get("pDocument"),
                "pages_printed": int(row.get("PagesPrinted", 0)), "total_pages": int(row.get("TotalPages", 0)),
                "message": "Spooler observations are not a physical paper confirmation"}
