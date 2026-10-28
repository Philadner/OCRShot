package main

import (
	"errors"
	"fmt"
	"image"
	"image/draw"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"unicode/utf8"

	ort "github.com/yalue/onnxruntime_go"
	xdraw "golang.org/x/image/draw"
)

const (
	detInputName  = "x"
	detOutputName = "sigmoid_0.tmp_0"
	recInputName  = "x"
	recOutputName = "softmax_11.tmp_0"
)

var ortInitOnce sync.Once
var ortInitErr error

type OCREngine struct {
	mu       sync.Mutex
	provider string
	det      *ort.DynamicAdvancedSession
	rec      *ort.DynamicAdvancedSession
	chars    []string
}

type textBox struct {
	x0 int
	y0 int
	x1 int
	y1 int
}

func NewOCREngine() (*OCREngine, error) {
	modelDir, err := findOCRModelsDir()
	if err != nil {
		return nil, err
	}
	if err := initializeONNXRuntime(); err != nil {
		return nil, err
	}

	provider := "CPU"
	tryCUDA := strings.EqualFold(os.Getenv("OCRSHOT_OCR_PROVIDER"), "CUDA") ||
		(os.Getenv("OCRSHOT_DISABLE_CUDA") != "1" && !strings.EqualFold(os.Getenv("OCRSHOT_OCR_PROVIDER"), "CPU"))
	detPath := filepath.Join(modelDir, "ch_PP-OCRv4_det_infer.onnx")
	recPath := filepath.Join(modelDir, "ch_PP-OCRv4_rec_infer.onnx")

	det, rec, err := newOCRSessions(detPath, recPath, tryCUDA)
	if err != nil && tryCUDA {
		det, rec, err = newOCRSessions(detPath, recPath, false)
		provider = "CPU"
	} else if err == nil && tryCUDA {
		provider = "CUDA"
	}
	if err != nil {
		return nil, err
	}

	chars, err := loadCharacterList(rec)
	if err != nil {
		det.Destroy()
		rec.Destroy()
		return nil, err
	}
	return &OCREngine{provider: provider, det: det, rec: rec, chars: chars}, nil
}

func (e *OCREngine) ProviderLabel() string {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.provider
}

func (e *OCREngine) Close() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.det != nil {
		_ = e.det.Destroy()
		e.det = nil
	}
	if e.rec != nil {
		_ = e.rec.Destroy()
		e.rec = nil
	}
}

func (e *OCREngine) ReadText(img image.Image) (string, string, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.det == nil || e.rec == nil {
		return "", e.provider, errors.New("OCR engine is closed")
	}

	boxes, err := e.detectTextBoxes(img)
	if err != nil {
		return "", e.provider, err
	}
	if len(boxes) == 0 {
		return "", e.provider, nil
	}

	lines := make([]string, 0, len(boxes))
	for _, box := range boxes {
		crop := cropImage(img, box)
		text, score, err := e.recognizeLine(crop)
		if err != nil {
			return "", e.provider, err
		}
		if score >= 0.35 && strings.TrimSpace(text) != "" {
			lines = append(lines, text)
		}
	}
	return strings.Join(lines, "\n"), e.provider, nil
}

func initializeONNXRuntime() error {
	ortInitOnce.Do(func() {
		if dll := findONNXRuntimeDLL(); dll != "" {
			ort.SetSharedLibraryPath(dll)
			addPathDir(filepath.Dir(dll))
		}
		addNvidiaDLLDirs()
		ortInitErr = ort.InitializeEnvironment(ort.WithLogLevelWarning())
	})
	return ortInitErr
}

func newOCRSessions(detPath string, recPath string, cuda bool) (*ort.DynamicAdvancedSession, *ort.DynamicAdvancedSession, error) {
	options, err := newSessionOptions(cuda)
	if err != nil {
		return nil, nil, err
	}
	defer options.Destroy()

	det, err := ort.NewDynamicAdvancedSession(detPath, []string{detInputName}, []string{detOutputName}, options)
	if err != nil {
		return nil, nil, err
	}
	rec, err := ort.NewDynamicAdvancedSession(recPath, []string{recInputName}, []string{recOutputName}, options)
	if err != nil {
		_ = det.Destroy()
		return nil, nil, err
	}
	return det, rec, nil
}

func newSessionOptions(cuda bool) (*ort.SessionOptions, error) {
	options, err := ort.NewSessionOptions()
	if err != nil {
		return nil, err
	}
	_ = options.SetGraphOptimizationLevel(ort.GraphOptimizationLevelEnableAll)
	_ = options.SetCpuMemArena(false)
	_ = options.SetLogSeverityLevel(ort.LoggingLevelWarning)
	if cuda {
		cudaOptions, err := ort.NewCUDAProviderOptions()
		if err != nil {
			options.Destroy()
			return nil, err
		}
		defer cudaOptions.Destroy()
		_ = cudaOptions.Update(map[string]string{
			"device_id":                 "0",
			"arena_extend_strategy":     "kNextPowerOfTwo",
			"cudnn_conv_algo_search":    "EXHAUSTIVE",
			"do_copy_in_default_stream": "1",
		})
		if err := options.AppendExecutionProviderCUDA(cudaOptions); err != nil {
			options.Destroy()
			return nil, err
		}
	}
	return options, nil
}

func loadCharacterList(session *ort.DynamicAdvancedSession) ([]string, error) {
	meta, err := session.GetModelMetadata()
	if err != nil {
		return nil, err
	}
	defer meta.Destroy()
	raw, ok, err := meta.LookupCustomMetadataMap("character")
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, errors.New("recognition model does not contain character metadata")
	}
	lines := strings.Split(strings.ReplaceAll(raw, "\r\n", "\n"), "\n")
	chars := make([]string, 0, len(lines)+2)
	chars = append(chars, "blank")
	for _, line := range lines {
		line = strings.TrimSuffix(line, "\r")
		if line != "" {
			chars = append(chars, line)
		}
	}
	chars = append(chars, " ")
	return chars, nil
}

func (e *OCREngine) detectTextBoxes(img image.Image) ([]textBox, error) {
	src := rgbaImage(img)
	origW := src.Bounds().Dx()
	origH := src.Bounds().Dy()
	resizedW, resizedH := detResizeSize(origW, origH)
	resized := resizeRGBA(src, resizedW, resizedH)
	inputData := make([]float32, 1*3*resizedW*resizedH)
	fillNCHW(inputData, resized, resizedW, resizedH, 0.5, 0.5)

	input, err := ort.NewTensor(ort.NewShape(1, 3, int64(resizedH), int64(resizedW)), inputData)
	if err != nil {
		return nil, err
	}
	defer input.Destroy()

	outputs := []ort.Value{nil}
	if err := e.det.Run([]ort.Value{input}, outputs); err != nil {
		return nil, err
	}
	defer outputs[0].Destroy()
	output, ok := outputs[0].(*ort.Tensor[float32])
	if !ok {
		return nil, errors.New("detector returned unexpected output tensor type")
	}
	shape := output.GetShape()
	if len(shape) != 4 {
		return nil, fmt.Errorf("detector returned unexpected shape %v", shape)
	}
	outH, outW := int(shape[2]), int(shape[3])
	probs := output.GetData()
	mask := buildDilatedMask(probs, outW, outH, 0.3)
	boxes := connectedTextBoxes(mask, probs, outW, outH, origW, origH)
	sortTextBoxes(boxes)
	return boxes, nil
}

func (e *OCREngine) recognizeLine(img image.Image) (string, float32, error) {
	src := rgbaImage(img)
	w, h := src.Bounds().Dx(), src.Bounds().Dy()
	if w <= 0 || h <= 0 {
		return "", 0, nil
	}
	ratio := float64(w) / float64(h)
	targetH := 48
	targetW := int(math.Ceil(float64(targetH) * math.Max(320.0/48.0, ratio)))
	if targetW < 32 {
		targetW = 32
	}
	resizedW := int(math.Ceil(float64(targetH) * ratio))
	if resizedW > targetW {
		resizedW = targetW
	}
	resized := resizeRGBA(src, resizedW, targetH)

	inputData := make([]float32, 1*3*targetH*targetW)
	fillNCHW(inputData, resized, resizedW, targetH, 0.5, 0.5)
	input, err := ort.NewTensor(ort.NewShape(1, 3, int64(targetH), int64(targetW)), inputData)
	if err != nil {
		return "", 0, err
	}
	defer input.Destroy()

	outputs := []ort.Value{nil}
	if err := e.rec.Run([]ort.Value{input}, outputs); err != nil {
		return "", 0, err
	}
	defer outputs[0].Destroy()
	output, ok := outputs[0].(*ort.Tensor[float32])
	if !ok {
		return "", 0, errors.New("recognizer returned unexpected output tensor type")
	}
	return e.decodeCTC(output)
}

func (e *OCREngine) decodeCTC(output *ort.Tensor[float32]) (string, float32, error) {
	shape := output.GetShape()
	if len(shape) != 3 {
		return "", 0, fmt.Errorf("recognizer returned unexpected shape %v", shape)
	}
	steps := int(shape[1])
	classes := int(shape[2])
	data := output.GetData()
	var b strings.Builder
	var scores []float32
	prev := -1
	for step := 0; step < steps; step++ {
		base := step * classes
		bestIdx := 0
		bestScore := data[base]
		for idx := 1; idx < classes; idx++ {
			if data[base+idx] > bestScore {
				bestIdx = idx
				bestScore = data[base+idx]
			}
		}
		if bestIdx != 0 && bestIdx != prev && bestIdx < len(e.chars) {
			b.WriteString(e.chars[bestIdx])
			scores = append(scores, bestScore)
		}
		prev = bestIdx
	}
	if len(scores) == 0 {
		return "", 0, nil
	}
	var sum float32
	for _, score := range scores {
		sum += score
	}
	text := strings.ToValidUTF8(b.String(), "")
	if !utf8.ValidString(text) {
		text = ""
	}
	return text, sum / float32(len(scores)), nil
}

func detResizeSize(w, h int) (int, int) {
	ratio := 1.0
	limit := 736.0
	if math.Min(float64(w), float64(h)) < limit {
		if h < w {
			ratio = limit / float64(h)
		} else {
			ratio = limit / float64(w)
		}
	}
	resizeW := int(math.Round(float64(w)*ratio/32.0) * 32)
	resizeH := int(math.Round(float64(h)*ratio/32.0) * 32)
	if resizeW < 32 {
		resizeW = 32
	}
	if resizeH < 32 {
		resizeH = 32
	}
	return resizeW, resizeH
}

func rgbaImage(img image.Image) *image.RGBA {
	if rgba, ok := img.(*image.RGBA); ok && rgba.Bounds().Min.X == 0 && rgba.Bounds().Min.Y == 0 {
		return rgba
	}
	b := img.Bounds()
	out := image.NewRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
	draw.Draw(out, out.Bounds(), img, b.Min, draw.Src)
	return out
}

func resizeRGBA(src *image.RGBA, w, h int) *image.RGBA {
	out := image.NewRGBA(image.Rect(0, 0, w, h))
	xdraw.BiLinear.Scale(out, out.Bounds(), src, src.Bounds(), xdraw.Over, nil)
	return out
}

func fillNCHW(data []float32, img *image.RGBA, w, h int, mean, std float32) {
	stride := w * h
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			i := img.PixOffset(x, y)
			p := y*w + x
			data[p] = ((float32(img.Pix[i]) / 255.0) - mean) / std
			data[stride+p] = ((float32(img.Pix[i+1]) / 255.0) - mean) / std
			data[2*stride+p] = ((float32(img.Pix[i+2]) / 255.0) - mean) / std
		}
	}
}

func buildDilatedMask(probs []float32, w, h int, threshold float32) []bool {
	base := make([]bool, w*h)
	for i, p := range probs {
		if p > threshold {
			base[i] = true
		}
	}
	out := make([]bool, w*h)
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			if !base[y*w+x] {
				continue
			}
			for yy := max(0, y-2); yy <= min(h-1, y+2); yy++ {
				for xx := max(0, x-6); xx <= min(w-1, x+6); xx++ {
					out[yy*w+xx] = true
				}
			}
		}
	}
	return out
}

func connectedTextBoxes(mask []bool, probs []float32, w, h, destW, destH int) []textBox {
	visited := make([]bool, len(mask))
	var boxes []textBox
	queue := make([]int, 0, 1024)
	for i, on := range mask {
		if !on || visited[i] {
			continue
		}
		x0, y0 := w, h
		x1, y1 := 0, 0
		scoreSum := float32(0)
		scoreCount := 0
		queue = append(queue[:0], i)
		visited[i] = true
		for len(queue) > 0 {
			p := queue[len(queue)-1]
			queue = queue[:len(queue)-1]
			x, y := p%w, p/w
			if x < x0 {
				x0 = x
			}
			if x > x1 {
				x1 = x
			}
			if y < y0 {
				y0 = y
			}
			if y > y1 {
				y1 = y
			}
			scoreSum += probs[p]
			scoreCount++
			for _, n := range []int{p - 1, p + 1, p - w, p + w} {
				if n < 0 || n >= len(mask) || visited[n] || !mask[n] {
					continue
				}
				if (n == p-1 && x == 0) || (n == p+1 && x == w-1) {
					continue
				}
				visited[n] = true
				queue = append(queue, n)
			}
		}
		bw, bh := x1-x0+1, y1-y0+1
		if bw < 3 || bh < 3 || scoreCount == 0 || scoreSum/float32(scoreCount) < 0.12 {
			continue
		}
		padX := max(2, bw/8)
		padY := max(2, bh/5)
		x0 = max(0, x0-padX)
		x1 = min(w-1, x1+padX)
		y0 = max(0, y0-padY)
		y1 = min(h-1, y1+padY)
		boxes = append(boxes, textBox{
			x0: int(math.Round(float64(x0) / float64(w) * float64(destW))),
			y0: int(math.Round(float64(y0) / float64(h) * float64(destH))),
			x1: int(math.Round(float64(x1+1) / float64(w) * float64(destW))),
			y1: int(math.Round(float64(y1+1) / float64(h) * float64(destH))),
		})
	}
	return mergeTextBoxes(boxes)
}

func mergeTextBoxes(boxes []textBox) []textBox {
	sortTextBoxes(boxes)
	merged := make([]textBox, 0, len(boxes))
	for _, box := range boxes {
		if box.x1-box.x0 < 4 || box.y1-box.y0 < 4 {
			continue
		}
		if len(merged) == 0 {
			merged = append(merged, box)
			continue
		}
		last := &merged[len(merged)-1]
		lastH := last.y1 - last.y0
		boxH := box.y1 - box.y0
		verticalOverlap := min(last.y1, box.y1) - max(last.y0, box.y0)
		sameLine := verticalOverlap > min(lastH, boxH)/2
		closeGap := box.x0-last.x1 < max(16, min(lastH, boxH)*2)
		if sameLine && closeGap {
			last.x0 = min(last.x0, box.x0)
			last.y0 = min(last.y0, box.y0)
			last.x1 = max(last.x1, box.x1)
			last.y1 = max(last.y1, box.y1)
			continue
		}
		merged = append(merged, box)
	}
	return merged
}

func sortTextBoxes(boxes []textBox) {
	sort.Slice(boxes, func(i, j int) bool {
		iy := (boxes[i].y0 + boxes[i].y1) / 2
		jy := (boxes[j].y0 + boxes[j].y1) / 2
		if abs(iy-jy) > 12 {
			return iy < jy
		}
		return boxes[i].x0 < boxes[j].x0
	})
}

func cropImage(img image.Image, box textBox) image.Image {
	src := rgbaImage(img)
	b := src.Bounds()
	box.x0 = max(0, min(b.Dx()-1, box.x0))
	box.y0 = max(0, min(b.Dy()-1, box.y0))
	box.x1 = max(box.x0+1, min(b.Dx(), box.x1))
	box.y1 = max(box.y0+1, min(b.Dy(), box.y1))
	out := image.NewRGBA(image.Rect(0, 0, box.x1-box.x0, box.y1-box.y0))
	draw.Draw(out, out.Bounds(), src, image.Point{X: box.x0, Y: box.y0}, draw.Src)
	return out
}

func findOCRModelsDir() (string, error) {
	candidates := []string{}
	if env := os.Getenv("OCRSHOT_MODELS_DIR"); env != "" {
		candidates = append(candidates, env)
	}
	if exe, err := os.Executable(); err == nil {
		base := filepath.Dir(exe)
		candidates = append(candidates, filepath.Join(base, "models"))
	}
	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates,
			filepath.Join(cwd, "models"),
			filepath.Join(cwd, "..", ".venv", "Lib", "site-packages", "rapidocr_onnxruntime", "models"),
			filepath.Join(cwd, ".venv", "Lib", "site-packages", "rapidocr_onnxruntime", "models"),
		)
	}
	for _, candidate := range candidates {
		if hasOCRModels(candidate) {
			abs, _ := filepath.Abs(candidate)
			return abs, nil
		}
	}
	return "", errors.New("OCR models not found; set OCRSHOT_MODELS_DIR or copy RapidOCR ONNX models to backend/models")
}

func hasOCRModels(dir string) bool {
	if dir == "" {
		return false
	}
	for _, name := range []string{"ch_PP-OCRv4_det_infer.onnx", "ch_PP-OCRv4_rec_infer.onnx"} {
		if _, err := os.Stat(filepath.Join(dir, name)); err != nil {
			return false
		}
	}
	return true
}

func findONNXRuntimeDLL() string {
	candidates := []string{}
	if env := os.Getenv("OCRSHOT_ORT_DLL"); env != "" {
		candidates = append(candidates, env)
	}
	if exe, err := os.Executable(); err == nil {
		base := filepath.Dir(exe)
		candidates = append(candidates, filepath.Join(base, "onnxruntime.dll"), filepath.Join(base, "onnxruntime", "capi", "onnxruntime.dll"))
	}
	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates,
			filepath.Join(cwd, "onnxruntime.dll"),
			filepath.Join(cwd, "..", ".venv", "Lib", "site-packages", "onnxruntime", "capi", "onnxruntime.dll"),
			filepath.Join(cwd, ".venv", "Lib", "site-packages", "onnxruntime", "capi", "onnxruntime.dll"),
		)
	}
	for _, candidate := range candidates {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			abs, _ := filepath.Abs(candidate)
			return abs
		}
	}
	return ""
}

func addNvidiaDLLDirs() {
	candidates := []string{}
	if cwd, err := os.Getwd(); err == nil {
		candidates = append(candidates,
			filepath.Join(cwd, "..", ".venv", "Lib", "site-packages", "nvidia"),
			filepath.Join(cwd, ".venv", "Lib", "site-packages", "nvidia"),
		)
	}
	for _, root := range candidates {
		entries, err := os.ReadDir(root)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			bin := filepath.Join(root, entry.Name(), "bin")
			if info, err := os.Stat(bin); err == nil && info.IsDir() {
				addPathDir(bin)
			}
		}
	}
}

func addPathDir(dir string) {
	if dir == "" {
		return
	}
	path := os.Getenv("PATH")
	lowerDir := strings.ToLower(dir)
	for _, part := range strings.Split(path, string(os.PathListSeparator)) {
		if strings.ToLower(part) == lowerDir {
			return
		}
	}
	_ = os.Setenv("PATH", dir+string(os.PathListSeparator)+path)
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}
