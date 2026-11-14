// Optional fixture capture using Loki's public parser and RE2 matchers.
package main

import (
	"encoding/json"
	"os"

	"github.com/grafana/loki/v3/pkg/logql/syntax"
)

func main() {
	var fixture map[string]interface{}
	input, err := os.ReadFile("input.json")
	if err != nil {
		panic(err)
	}
	if err := json.Unmarshal(input, &fixture); err != nil {
		panic(err)
	}
	for _, value := range fixture["cases"].([]interface{}) {
		c := value.(map[string]interface{})
		parsed, err := syntax.ParseExpr(c["expr"].(string))
		c["parse_ok"] = err == nil
		if err != nil {
			continue
		}
		selector := parsed.(syntax.LogSelectorExpr)
		matchers := selector.Matchers()
		patterns := map[string]string{}
		for _, m := range matchers {
			patterns[m.Name] = m.Value
		}
		c["patterns"] = patterns
		streams := []map[string]string{}
		matches := []int{}
		for _, v := range c["values"].([]interface{}) {
			streams = append(streams, map[string]string{"source_type": "journald", "host": "host-a", "service": v.(string), "level": "info"})
		}
		streams = append(streams, map[string]string{"source_type": "journald", "host": "host-a", "service": "unselected-service", "level": "info"})
		streams = append(streams, map[string]string{"source_type": "other", "host": "host-a", "service": "unselected-service", "level": "info"})
		for i, stream := range streams {
			matched := true
			for _, m := range matchers {
				matched = matched && m.Matches(stream[m.Name])
			}
			if matched {
				matches = append(matches, i)
			}
		}
		c["streams"] = streams
		c["matches"] = matches
	}
	output, err := json.MarshalIndent(fixture, "", "  ")
	if err != nil {
		panic(err)
	}
	if _, err := os.Stdout.Write(output); err != nil {
		panic(err)
	}
}
