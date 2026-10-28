package main

import "testing"

func TestSafeFilenamePiece(t *testing.T) {
	if got := safeFilenamePiece("  A/B:C*D?  "); got != "A_B_C_D" {
		t.Fatalf("safeFilenamePiece() = %q", got)
	}
	if got := safeFilenamePiece("..."); got != "window" {
		t.Fatalf("empty safeFilenamePiece() = %q", got)
	}
}

func TestContainsWord(t *testing.T) {
	if !containsWord("hello OCRShot world", "ocrshot", false) {
		t.Fatal("expected case-insensitive whole-word match")
	}
	if containsWord("hello OCRShotting world", "OCRShot", true) {
		t.Fatal("expected whole-word matching")
	}
	if containsWord("hello OCRShot world", "ocrshot", true) {
		t.Fatal("expected case-sensitive mismatch")
	}
}
