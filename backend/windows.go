package main

import (
	"errors"
	"fmt"
	"image"
	"image/color"
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

const (
	gaRoot              = 2
	pwRenderFullContent = 0x00000002
	swRestore           = 9
	swShow              = 5
	processQueryLimited = 0x1000
	biRGB               = 0
	dibRGBColors        = 0
)

var (
	user32   = syscall.NewLazyDLL("user32.dll")
	gdi32    = syscall.NewLazyDLL("gdi32.dll")
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	shell32  = syscall.NewLazyDLL("shell32.dll")

	procIsWindow                   = user32.NewProc("IsWindow")
	procIsWindowVisible            = user32.NewProc("IsWindowVisible")
	procIsIconic                   = user32.NewProc("IsIconic")
	procGetWindowTextLengthW       = user32.NewProc("GetWindowTextLengthW")
	procGetWindowTextW             = user32.NewProc("GetWindowTextW")
	procGetWindowRect              = user32.NewProc("GetWindowRect")
	procGetCursorPos               = user32.NewProc("GetCursorPos")
	procWindowFromPoint            = user32.NewProc("WindowFromPoint")
	procGetAncestor                = user32.NewProc("GetAncestor")
	procEnumWindows                = user32.NewProc("EnumWindows")
	procGetWindowThreadProcessID   = user32.NewProc("GetWindowThreadProcessId")
	procGetWindowDC                = user32.NewProc("GetWindowDC")
	procReleaseDC                  = user32.NewProc("ReleaseDC")
	procPrintWindow                = user32.NewProc("PrintWindow")
	procSetForegroundWindow        = user32.NewProc("SetForegroundWindow")
	procShowWindow                 = user32.NewProc("ShowWindow")
	procAllowSetForegroundWindow   = user32.NewProc("AllowSetForegroundWindow")
	procCreateCompatibleDC         = gdi32.NewProc("CreateCompatibleDC")
	procCreateCompatibleBitmap     = gdi32.NewProc("CreateCompatibleBitmap")
	procSelectObject               = gdi32.NewProc("SelectObject")
	procDeleteObject               = gdi32.NewProc("DeleteObject")
	procDeleteDC                   = gdi32.NewProc("DeleteDC")
	procGetDIBits                  = gdi32.NewProc("GetDIBits")
	procOpenProcess                = kernel32.NewProc("OpenProcess")
	procQueryFullProcessImageNameW = kernel32.NewProc("QueryFullProcessImageNameW")
	procCloseHandle                = kernel32.NewProc("CloseHandle")
	procShellExecuteW              = shell32.NewProc("ShellExecuteW")
)

type winRect struct {
	Left   int32
	Top    int32
	Right  int32
	Bottom int32
}

type winPoint struct {
	X int32
	Y int32
}

type bitmapInfoHeader struct {
	Size          uint32
	Width         int32
	Height        int32
	Planes        uint16
	BitCount      uint16
	Compression   uint32
	SizeImage     uint32
	XPelsPerMeter int32
	YPelsPerMeter int32
	ClrUsed       uint32
	ClrImportant  uint32
}

type bitmapInfo struct {
	Header bitmapInfoHeader
}

func getWindowInfo(hwnd int64) (WindowInfo, error) {
	if !isSelectableWindow(hwnd) {
		return WindowInfo{}, errors.New("the selected window is no longer available")
	}
	rect, err := getRect(hwnd)
	if err != nil {
		return WindowInfo{}, err
	}
	pid, processName := getProcessInfo(hwnd)
	return WindowInfo{
		HWND:        hwnd,
		Title:       getWindowTitle(hwnd),
		ProcessID:   pid,
		ProcessName: processName,
		Rect: Rect{
			Left:   int(rect.Left),
			Top:    int(rect.Top),
			Right:  int(rect.Right),
			Bottom: int(rect.Bottom),
		},
	}, nil
}

func windowAtCursor() (WindowInfo, error) {
	var point winPoint
	r1, _, err := procGetCursorPos.Call(uintptr(unsafe.Pointer(&point)))
	if r1 == 0 {
		return WindowInfo{}, err
	}
	hwnd, _, _ := procWindowFromPoint.Call(uintptr(uint32(point.X)) | uintptr(uint32(point.Y))<<32)
	if hwnd == 0 {
		return WindowInfo{}, errors.New("no window under cursor")
	}
	root, _, _ := procGetAncestor.Call(hwnd, gaRoot)
	if root == 0 {
		root = hwnd
	}
	return getWindowInfo(int64(root))
}

func findRememberedWindow(hwnd *int64, title string, processName string) (WindowInfo, error) {
	if hwnd != nil && isSelectableWindow(*hwnd) {
		return getWindowInfo(*hwnd)
	}
	var found int64
	cb := syscall.NewCallback(func(raw uintptr, _ uintptr) uintptr {
		current := int64(raw)
		if !isSelectableWindow(current) {
			return 1
		}
		info, err := getWindowInfo(current)
		if err != nil {
			return 1
		}
		sameTitle := title != "" && info.Title == title
		sameProcess := processName != "" && info.ProcessName == processName
		if sameTitle && (sameProcess || processName == "") {
			found = current
			return 0
		}
		return 1
	})
	procEnumWindows.Call(cb, 0)
	if found == 0 {
		return WindowInfo{}, errors.New("remembered window was not found")
	}
	return getWindowInfo(found)
}

func isSelectableWindow(hwnd int64) bool {
	if hwnd == 0 {
		return false
	}
	if ok, _, _ := procIsWindow.Call(uintptr(hwnd)); ok == 0 {
		return false
	}
	if ok, _, _ := procIsWindowVisible.Call(uintptr(hwnd)); ok == 0 {
		return false
	}
	if iconic, _, _ := procIsIconic.Call(uintptr(hwnd)); iconic != 0 {
		return false
	}
	if getWindowTitle(hwnd) == "" {
		return false
	}
	rect, err := getRect(hwnd)
	if err != nil {
		return false
	}
	return rect.Right-rect.Left > 20 && rect.Bottom-rect.Top > 20
}

func getRect(hwnd int64) (winRect, error) {
	var rect winRect
	r1, _, err := procGetWindowRect.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&rect)))
	if r1 == 0 {
		return rect, err
	}
	return rect, nil
}

func getWindowTitle(hwnd int64) string {
	length, _, _ := procGetWindowTextLengthW.Call(uintptr(hwnd))
	if length == 0 {
		return ""
	}
	buffer := make([]uint16, length+1)
	procGetWindowTextW.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&buffer[0])), uintptr(len(buffer)))
	return syscall.UTF16ToString(buffer)
}

func getProcessInfo(hwnd int64) (int64, string) {
	var pid uint32
	procGetWindowThreadProcessID.Call(uintptr(hwnd), uintptr(unsafe.Pointer(&pid)))
	if pid == 0 {
		return 0, ""
	}
	handle, _, _ := procOpenProcess.Call(processQueryLimited, 0, uintptr(pid))
	if handle == 0 {
		return int64(pid), ""
	}
	defer procCloseHandle.Call(handle)
	size := uint32(32768)
	buffer := make([]uint16, size)
	r1, _, _ := procQueryFullProcessImageNameW.Call(handle, 0, uintptr(unsafe.Pointer(&buffer[0])), uintptr(unsafe.Pointer(&size)))
	if r1 == 0 {
		return int64(pid), ""
	}
	return int64(pid), filepath.Base(syscall.UTF16ToString(buffer[:size]))
}

func captureWindow(hwnd int64) (image.Image, string, error) {
	if !isSelectableWindow(hwnd) {
		return nil, "", errors.New("the selected window is no longer available")
	}
	rect, err := getRect(hwnd)
	if err != nil {
		return nil, "", err
	}
	width := int(rect.Right - rect.Left)
	height := int(rect.Bottom - rect.Top)
	if width <= 0 || height <= 0 {
		return nil, "", errors.New("the selected window has an invalid size")
	}

	windowDC, _, _ := procGetWindowDC.Call(uintptr(hwnd))
	if windowDC == 0 {
		return nil, "", errors.New("could not get window device context")
	}
	defer procReleaseDC.Call(uintptr(hwnd), windowDC)

	memDC, _, _ := procCreateCompatibleDC.Call(windowDC)
	if memDC == 0 {
		return nil, "", errors.New("could not create compatible device context")
	}
	defer procDeleteDC.Call(memDC)

	bitmap, _, _ := procCreateCompatibleBitmap.Call(windowDC, uintptr(width), uintptr(height))
	if bitmap == 0 {
		return nil, "", errors.New("could not create compatible bitmap")
	}
	defer procDeleteObject.Call(bitmap)

	oldObject, _, _ := procSelectObject.Call(memDC, bitmap)
	defer procSelectObject.Call(memDC, oldObject)

	ok, _, _ := procPrintWindow.Call(uintptr(hwnd), memDC, pwRenderFullContent)
	if ok != 1 {
		return nil, "", errors.New("Windows could not capture that window with PrintWindow")
	}

	info := bitmapInfo{
		Header: bitmapInfoHeader{
			Size:        uint32(unsafe.Sizeof(bitmapInfoHeader{})),
			Width:       int32(width),
			Height:      -int32(height),
			Planes:      1,
			BitCount:    32,
			Compression: biRGB,
			SizeImage:   uint32(width * height * 4),
		},
	}
	pixels := make([]byte, width*height*4)
	lines, _, _ := procGetDIBits.Call(memDC, bitmap, 0, uintptr(height), uintptr(unsafe.Pointer(&pixels[0])), uintptr(unsafe.Pointer(&info)), dibRGBColors)
	if lines == 0 {
		return nil, "", errors.New("could not read captured bitmap pixels")
	}

	img := image.NewRGBA(image.Rect(0, 0, width, height))
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			i := (y*width + x) * 4
			img.SetRGBA(x, y, color.RGBA{R: pixels[i+2], G: pixels[i+1], B: pixels[i], A: 255})
		}
	}
	return img, getWindowTitle(hwnd), nil
}

func openPath(path string) error {
	if _, err := os.Stat(path); err != nil {
		return err
	}
	verb, _ := syscall.UTF16PtrFromString("open")
	file, _ := syscall.UTF16PtrFromString(path)
	r1, _, err := procShellExecuteW.Call(0, uintptr(unsafe.Pointer(verb)), uintptr(unsafe.Pointer(file)), 0, 0, swShow)
	if r1 <= 32 {
		return fmt.Errorf("could not open %s: %v", path, err)
	}
	return nil
}

func activateWindow(hwnd int64) bool {
	if hwnd == 0 {
		return false
	}
	iconic, _, _ := procIsIconic.Call(uintptr(hwnd))
	if iconic != 0 {
		procShowWindow.Call(uintptr(hwnd), swRestore)
	}
	procAllowSetForegroundWindow.Call(^uintptr(0))
	ok, _, _ := procSetForegroundWindow.Call(uintptr(hwnd))
	if ok != 0 {
		return true
	}
	procShowWindow.Call(uintptr(hwnd), swShow)
	ok, _, _ = procSetForegroundWindow.Call(uintptr(hwnd))
	return ok != 0
}
