// count_lines prints how many lines of each file type a folder contains.
//
//	go run count_lines.go -dir ./src -ext .go,.py
package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func main() {
	dir := flag.String("dir", ".", "folder to scan")
	exts := flag.String("ext", "", "comma separated extensions to include (default: all)")
	flag.Parse()

	wanted := map[string]bool{}
	for _, e := range strings.Split(*exts, ",") {
		if e != "" {
			wanted[e] = true
		}
	}
	totals := map[string]int{}
	filepath.Walk(*dir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		ext := filepath.Ext(path)
		if len(wanted) > 0 && !wanted[ext] {
			return nil
		}
		f, err := os.Open(path)
		if err != nil {
			return nil
		}
		defer f.Close()
		scanner := bufio.NewScanner(f)
		for scanner.Scan() {
			totals[ext]++
		}
		return nil
	})
	keys := make([]string, 0, len(totals))
	for k := range totals {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		fmt.Printf("%-8s %8d\n", k, totals[k])
	}
}
