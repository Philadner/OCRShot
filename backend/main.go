package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"image"
	"image/png"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	ocrMetadataVersion = 1
)

type AppConfig struct {
	MonitorWord            string  `json:"monitor_word"`
	SelectedHWND           *int64  `json:"selected_hwnd"`
	SelectedTitle          string  `json:"selected_title"`
	SelectedProcessID      *int64  `json:"selected_process_id"`
	SelectedProcessName    string  `json:"selected_process_name"`
	ChatGPTHWND            *int64  `json:"chatgpt_hwnd"`
	ChatGPTTitle           string  `json:"chatgpt_title"`
	ChatGPTProcessID       *int64  `json:"chatgpt_process_id"`
	ChatGPTProcessName     string  `json:"chatgpt_process_name"`
	ScanIntervalSeconds    float64 `json:"scan_interval_seconds"`
	TriggerCooldownSeconds float64 `json:"trigger_cooldown_seconds"`
	MatchCase              bool    `json:"match_case"`
}

type Rect struct {
	Left   int `json:"left"`
	Top    int `json:"top"`
	Right  int `json:"right"`
	Bottom int `json:"bottom"`
}

type WindowInfo struct {
	HWND        int64  `json:"hwnd"`
	Title       string `json:"title"`
	ProcessID   int64  `json:"process_id"`
	ProcessName string `json:"process_name"`
	Rect        Rect   `json:"rect"`
}

type HistoryItem struct {
	ImagePath    string `json:"image_path"`
	MetadataPath string `json:"metadata_path"`
	ModifiedAt   string `json:"modified_at"`
	WindowTitle  string `json:"window_title"`
	HasOCRText   bool   `json:"has_ocr_text"`
}

type rpcRequest struct {
	ID     string          `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}

type rpcResponse struct {
	ID     string    `json:"id"`
	Result any       `json:"result,omitempty"`
	Error  *rpcError `json:"error"`
}

type rpcError struct {
	Message string `json:"message"`
}

type rpcEvent struct {
	Event   string `json:"event"`
	Payload any    `json:"payload"`
}

type Server struct {
	mu              sync.Mutex
	outMu           sync.Mutex
	out             io.Writer
	config          AppConfig
	appDir          string
	screenshotDir   string
	configPath      string
	lastSnapshotPNG []byte
	lastHadMatch    bool
	lastTriggerAt   time.Time
	monitorCancel   context.CancelFunc
	ocr             *OCREngine
}

func main() {
	server, err := NewServer(os.Stdout)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	defer server.Close()

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 0, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		var req rpcRequest
		if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
			server.writeResponse("", nil, err)
			continue
		}
		result, err := server.Handle(req.Method, req.Params)
		server.writeResponse(req.ID, result, err)
	}
}

func NewServer(out io.Writer) (*Server, error) {
	appDir := filepath.Join(localAppData(), "Philadner", "OCRShot")
	screenshotDir := filepath.Join(appDir, "Screenshots")
	if err := os.MkdirAll(screenshotDir, 0o755); err != nil {
		return nil, err
	}
	s := &Server{
		out:           out,
		appDir:        appDir,
		screenshotDir: screenshotDir,
		configPath:    filepath.Join(appDir, "config.json"),
		config: AppConfig{
			ScanIntervalSeconds:    3.0,
			TriggerCooldownSeconds: 10.0,
		},
	}
	if cfg, err := s.loadConfig(); err == nil {
		s.config = cfg
	}
	ocr, err := NewOCREngine()
	if err != nil {
		fmt.Fprintf(os.Stderr, "OCR engine unavailable: %v\n", err)
	} else {
		s.ocr = ocr
	}
	return s, nil
}

func (s *Server) Close() {
	s.stopMonitoring()
	if s.ocr != nil {
		s.ocr.Close()
	}
}

func (s *Server) Handle(method string, params json.RawMessage) (any, error) {
	switch method {
	case "GetConfig":
		s.mu.Lock()
		defer s.mu.Unlock()
		return s.config, nil
	case "UpdateConfig":
		var cfg AppConfig
		if err := json.Unmarshal(params, &cfg); err != nil {
			return nil, err
		}
		return cfg, s.saveConfig(cfg)
	case "GetState":
		s.mu.Lock()
		defer s.mu.Unlock()
		return map[string]any{
			"app_dir":        s.appDir,
			"screenshot_dir": s.screenshotDir,
			"monitoring":     s.monitorCancel != nil,
			"ocr_provider":   s.ocrProviderLabel(),
		}, nil
	case "Shutdown":
		s.Close()
		return map[string]any{"shutdown": true}, nil
	case "GetWindowAtCursor":
		return windowAtCursor()
	case "GetRememberedWindow":
		var in struct {
			HWND        *int64 `json:"hwnd"`
			Title       string `json:"title"`
			ProcessName string `json:"process_name"`
		}
		_ = json.Unmarshal(params, &in)
		return findRememberedWindow(in.HWND, in.Title, in.ProcessName)
	case "GetWindowInfo":
		var in struct {
			HWND int64 `json:"hwnd"`
		}
		if err := json.Unmarshal(params, &in); err != nil {
			return nil, err
		}
		return getWindowInfo(in.HWND)
	case "CaptureSelected":
		var in struct {
			HWND int64  `json:"hwnd"`
			Word string `json:"word"`
			Save bool   `json:"save"`
		}
		if err := json.Unmarshal(params, &in); err != nil {
			return nil, err
		}
		return s.captureSelected(in.HWND, in.Word, in.Save)
	case "StartMonitoring":
		var cfg AppConfig
		if err := json.Unmarshal(params, &cfg); err != nil {
			return nil, err
		}
		if cfg.SelectedHWND == nil {
			return nil, errors.New("select a window before starting monitoring")
		}
		if strings.TrimSpace(cfg.MonitorWord) == "" {
			return nil, errors.New("enter a word to monitor before starting")
		}
		if cfg.ScanIntervalSeconds <= 0 {
			cfg.ScanIntervalSeconds = 3
		}
		if err := s.saveConfig(cfg); err != nil {
			return nil, err
		}
		s.startMonitoring(cfg)
		return map[string]any{"monitoring": true}, nil
	case "StopMonitoring":
		s.stopMonitoring()
		return map[string]any{"monitoring": false}, nil
	case "ListHistory":
		return s.listHistory("")
	case "SearchHistory":
		var in struct {
			Query string `json:"query"`
		}
		_ = json.Unmarshal(params, &in)
		return s.listHistory(in.Query)
	case "DeleteHistory":
		var in struct {
			ImagePath string `json:"image_path"`
		}
		if err := json.Unmarshal(params, &in); err != nil {
			return nil, err
		}
		return nil, s.deleteHistory(in.ImagePath)
	case "OpenHistoryImage":
		var in struct {
			ImagePath string `json:"image_path"`
		}
		if err := json.Unmarshal(params, &in); err != nil {
			return nil, err
		}
		return nil, openPath(in.ImagePath)
	case "SelectChatGPTWindow":
		var in struct {
			HWND int64 `json:"hwnd"`
		}
		if err := json.Unmarshal(params, &in); err != nil {
			return nil, err
		}
		info, err := getWindowInfo(in.HWND)
		if err != nil {
			return nil, err
		}
		s.mu.Lock()
		cfg := s.config
		cfg.ChatGPTHWND = int64Ptr(info.HWND)
		cfg.ChatGPTTitle = info.Title
		cfg.ChatGPTProcessID = int64Ptr(info.ProcessID)
		cfg.ChatGPTProcessName = info.ProcessName
		s.mu.Unlock()
		return info, s.saveConfig(cfg)
	case "SendLastSnapshotToChatGPT":
		return nil, errors.New("ChatGPT paste automation still uses the Python frontend fallback")
	default:
		return nil, fmt.Errorf("unknown method %q", method)
	}
}

func (s *Server) writeResponse(id string, result any, err error) {
	resp := rpcResponse{ID: id, Result: result}
	if err != nil {
		resp.Error = &rpcError{Message: err.Error()}
	}
	s.writeJSON(resp)
}

func (s *Server) emit(event string, payload any) {
	s.writeJSON(rpcEvent{Event: event, Payload: payload})
}

func (s *Server) writeJSON(value any) {
	s.outMu.Lock()
	defer s.outMu.Unlock()
	data, err := json.Marshal(value)
	if err != nil {
		fmt.Fprintf(os.Stderr, "marshal rpc: %v\n", err)
		return
	}
	_, _ = s.out.Write(append(data, '\n'))
}

func (s *Server) loadConfig() (AppConfig, error) {
	data, err := os.ReadFile(s.configPath)
	if err != nil {
		return s.config, err
	}
	cfg := s.config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return s.config, err
	}
	return cfg, nil
}

func (s *Server) saveConfig(cfg AppConfig) error {
	if err := os.MkdirAll(s.appDir, 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(s.configPath, data, 0o644); err != nil {
		return err
	}
	s.mu.Lock()
	s.config = cfg
	s.mu.Unlock()
	return nil
}

func (s *Server) captureSelected(hwnd int64, word string, save bool) (map[string]any, error) {
	img, title, err := captureWindow(hwnd)
	if err != nil {
		return nil, err
	}
	pngBytes, err := encodePNG(img)
	if err != nil {
		return nil, err
	}
	s.mu.Lock()
	s.lastSnapshotPNG = append([]byte(nil), pngBytes...)
	s.mu.Unlock()

	result := map[string]any{
		"preview_png_base64": base64.StdEncoding.EncodeToString(pngBytes),
		"window_title":       title,
	}
	if save {
		path, err := s.saveScreenshot(img, hwnd, word, "")
		if err != nil {
			return nil, err
		}
		result["screenshot_path"] = path
	}
	return result, nil
}

func (s *Server) startMonitoring(cfg AppConfig) {
	s.stopMonitoring()
	ctx, cancel := context.WithCancel(context.Background())
	s.mu.Lock()
	s.monitorCancel = cancel
	s.lastHadMatch = false
	s.lastTriggerAt = time.Time{}
	s.mu.Unlock()

	interval := time.Duration(cfg.ScanIntervalSeconds * float64(time.Second))
	go func() {
		s.scanOnce(cfg)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.scanOnce(cfg)
			}
		}
	}()
}

func (s *Server) stopMonitoring() {
	s.mu.Lock()
	cancel := s.monitorCancel
	s.monitorCancel = nil
	s.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *Server) scanOnce(cfg AppConfig) {
	if cfg.SelectedHWND == nil {
		return
	}
	img, title, err := captureWindow(*cfg.SelectedHWND)
	if err != nil {
		s.emit("scanFailed", map[string]any{"message": err.Error()})
		return
	}
	pngBytes, _ := encodePNG(img)
	s.mu.Lock()
	s.lastSnapshotPNG = append([]byte(nil), pngBytes...)
	s.mu.Unlock()
	s.emit("previewReady", map[string]any{
		"png_base64": base64.StdEncoding.EncodeToString(pngBytes),
		"width":      img.Bounds().Dx(),
		"height":     img.Bounds().Dy(),
	})

	if s.ocr == nil {
		s.emit("scanFailed", map[string]any{"message": "Go OCR engine is unavailable. Check ONNX Runtime DLL and OCR model paths."})
		return
	}
	text, provider, err := s.ocr.ReadText(img)
	if err != nil {
		s.emit("scanFailed", map[string]any{"message": err.Error()})
		return
	}

	matched := containsWord(text, cfg.MonitorWord, cfg.MatchCase)
	now := time.Now()
	shouldSave := false
	s.mu.Lock()
	if matched && (!s.lastHadMatch || now.Sub(s.lastTriggerAt).Seconds() >= cfg.TriggerCooldownSeconds) {
		shouldSave = true
		s.lastTriggerAt = now
	}
	s.lastHadMatch = matched
	s.mu.Unlock()

	screenshotPath := ""
	if shouldSave {
		path, err := s.saveScreenshot(img, *cfg.SelectedHWND, cfg.MonitorWord, text)
		if err != nil {
			s.emit("scanFailed", map[string]any{"message": err.Error()})
			return
		}
		screenshotPath = path
	}
	s.emit("scanCompleted", map[string]any{
		"text":            text,
		"screenshot_path": screenshotPath,
		"scan_mode":       "rebuilt",
		"changed_count":   16,
		"provider":        provider,
		"window_title":    title,
	})
}

func (s *Server) saveScreenshot(img image.Image, hwnd int64, word string, ocrText string) (string, error) {
	if err := os.MkdirAll(s.screenshotDir, 0o755); err != nil {
		return "", err
	}
	title := "window"
	if info, err := getWindowInfo(hwnd); err == nil && strings.TrimSpace(info.Title) != "" {
		title = info.Title
	}
	filename := fmt.Sprintf("%s_%s_%s.png", time.Now().Format("20060102_150405"), safeFilenamePiece(title), safeFilenamePiece(word))
	path := filepath.Join(s.screenshotDir, filename)
	file, err := os.Create(path)
	if err != nil {
		return "", err
	}
	if err := png.Encode(file, img); err != nil {
		_ = file.Close()
		return "", err
	}
	if err := file.Close(); err != nil {
		return "", err
	}
	if ocrText != "" {
		if err := writeOCRMetadata(path, ocrText, title); err != nil {
			return "", err
		}
	}
	return path, nil
}

func (s *Server) ocrProviderLabel() string {
	if s.ocr == nil {
		return "unavailable"
	}
	return s.ocr.ProviderLabel()
}

func (s *Server) listHistory(query string) ([]HistoryItem, error) {
	matches, err := filepath.Glob(filepath.Join(s.screenshotDir, "*.png"))
	if err != nil {
		return nil, err
	}
	sort.Slice(matches, func(i, j int) bool {
		left, _ := os.Stat(matches[i])
		right, _ := os.Stat(matches[j])
		if left == nil || right == nil {
			return matches[i] < matches[j]
		}
		return left.ModTime().After(right.ModTime())
	})
	terms := strings.Fields(strings.ToLower(query))
	items := make([]HistoryItem, 0, len(matches))
	for _, path := range matches {
		meta, _ := readOCRMetadata(path)
		text, _ := meta["ocr_text"].(string)
		if len(terms) > 0 {
			lower := strings.ToLower(text)
			ok := true
			for _, term := range terms {
				if !strings.Contains(lower, term) {
					ok = false
					break
				}
			}
			if !ok {
				continue
			}
		}
		stat, err := os.Stat(path)
		if err != nil {
			continue
		}
		title, _ := meta["window_title"].(string)
		items = append(items, HistoryItem{
			ImagePath:    path,
			MetadataPath: metadataPath(path),
			ModifiedAt:   stat.ModTime().Format(time.RFC3339),
			WindowTitle:  title,
			HasOCRText:   text != "",
		})
	}
	return items, nil
}

func (s *Server) deleteHistory(imagePath string) error {
	if !isUnderDir(imagePath, s.screenshotDir) {
		return errors.New("history path is outside the screenshot directory")
	}
	if err := os.Remove(imagePath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	meta := metadataPath(imagePath)
	if err := os.Remove(meta); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func encodePNG(img image.Image) ([]byte, error) {
	var b bytes.Buffer
	err := png.Encode(&b, img)
	return b.Bytes(), err
}

func metadataPath(imagePath string) string {
	return imagePath + ".ocr.json"
}

func writeOCRMetadata(imagePath string, text string, windowTitle string) error {
	meta := map[string]any{
		"version":      ocrMetadataVersion,
		"image":        filepath.Base(imagePath),
		"window_title": windowTitle,
		"ocr_text":     text,
		"indexed_at":   time.Now().Format("2006-01-02T15:04:05"),
	}
	data, err := json.MarshalIndent(meta, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(metadataPath(imagePath), data, 0o644)
}

func readOCRMetadata(imagePath string) (map[string]any, error) {
	data, err := os.ReadFile(metadataPath(imagePath))
	if err != nil {
		return map[string]any{}, err
	}
	var meta map[string]any
	if err := json.Unmarshal(data, &meta); err != nil {
		return map[string]any{}, err
	}
	return meta, nil
}

var filenameCleaner = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

func safeFilenamePiece(value string) string {
	cleaned := filenameCleaner.ReplaceAllString(strings.TrimSpace(value), "_")
	cleaned = strings.Trim(cleaned, "._")
	if len(cleaned) > 48 {
		cleaned = cleaned[:48]
	}
	if cleaned == "" {
		return "window"
	}
	return cleaned
}

func containsWord(text string, word string, matchCase bool) bool {
	if strings.TrimSpace(word) == "" {
		return false
	}
	pattern := `\b` + regexp.QuoteMeta(word) + `\b`
	if !matchCase {
		pattern = `(?i)` + pattern
	}
	return regexp.MustCompile(pattern).FindStringIndex(text) != nil
}

func localAppData() string {
	if value := os.Getenv("LOCALAPPDATA"); value != "" {
		return value
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "."
	}
	return home
}

func int64Ptr(value int64) *int64 {
	return &value
}

func isUnderDir(path string, dir string) bool {
	absPath, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	absDir, err := filepath.Abs(dir)
	if err != nil {
		return false
	}
	rel, err := filepath.Rel(absDir, absPath)
	return err == nil && rel != "." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) && rel != ".."
}
