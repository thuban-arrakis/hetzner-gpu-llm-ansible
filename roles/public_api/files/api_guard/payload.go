package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"strconv"
	"strings"
)

var errInputLimit = errors.New("input limit")

func parseObject(raw []byte) (map[string]any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	value, err := readValue(dec, 0)
	if err != nil {
		return nil, err
	}
	if _, err = dec.Token(); err != io.EOF {
		return nil, errors.New("trailing JSON")
	}
	object, ok := value.(map[string]any)
	if !ok {
		return nil, errors.New("object required")
	}
	return object, nil
}
func readValue(dec *json.Decoder, depth int) (any, error) {
	if depth > 12 {
		return nil, errors.New("JSON nesting limit")
	}
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch delimiter := token.(type) {
	case json.Delim:
		switch delimiter {
		case '{':
			object := map[string]any{}
			for dec.More() {
				keyToken, err := dec.Token()
				if err != nil {
					return nil, err
				}
				key, ok := keyToken.(string)
				if !ok {
					return nil, errors.New("invalid key")
				}
				if _, exists := object[key]; exists {
					return nil, errors.New("duplicate key")
				}
				value, err := readValue(dec, depth+1)
				if err != nil {
					return nil, err
				}
				object[key] = value
				if len(object) > 64 {
					return nil, errors.New("field limit")
				}
			}
			close, err := dec.Token()
			if err != nil || close != json.Delim('}') {
				return nil, errors.New("invalid object")
			}
			return object, nil
		case '[':
			array := []any{}
			for dec.More() {
				value, err := readValue(dec, depth+1)
				if err != nil {
					return nil, err
				}
				array = append(array, value)
				if len(array) > 64 {
					return nil, errors.New("array limit")
				}
			}
			close, err := dec.Token()
			if err != nil || close != json.Delim(']') {
				return nil, errors.New("invalid array")
			}
			return array, nil
		default:
			return nil, errors.New("unexpected delimiter")
		}
	default:
		return token, nil
	}
}
func only(object map[string]any, allowed ...string) error {
	names := map[string]bool{}
	for _, key := range allowed {
		names[key] = true
	}
	for key := range object {
		if !names[key] {
			return errors.New("unsupported field")
		}
	}
	return nil
}
func integer(value any, min, max int64) (int64, error) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, errors.New("integer required")
	}
	parsed, err := strconv.ParseInt(string(number), 10, 64)
	if err != nil || parsed < min || parsed > max {
		return 0, errors.New("integer outside limit")
	}
	return parsed, nil
}
func decimal(value any, min, max float64) (float64, error) {
	number, ok := value.(json.Number)
	if !ok {
		return 0, errors.New("number required")
	}
	parsed, err := number.Float64()
	if err != nil || math.IsNaN(parsed) || math.IsInf(parsed, 0) || parsed < min || parsed > max {
		return 0, errors.New("number outside limit")
	}
	return parsed, nil
}
func booleanField(object map[string]any, key string, fallback bool) (bool, error) {
	value, exists := object[key]
	if !exists {
		return fallback, nil
	}
	result, ok := value.(bool)
	if !ok {
		return false, errors.New("boolean required")
	}
	return result, nil
}
func text(object map[string]any, key string, required bool, budget *int) (string, error) {
	value, exists := object[key]
	if !exists {
		if required {
			return "", errors.New("missing input")
		}
		return "", nil
	}
	result, ok := value.(string)
	if !ok {
		return "", errors.New("text required")
	}
	*budget -= len(result)
	if *budget < 0 {
		return "", errInputLimit
	}
	if required && strings.TrimSpace(result) == "" {
		return "", errors.New("empty input")
	}
	return result, nil
}
func messages(value any, c Config, budget *int) ([]map[string]string, error) {
	input, ok := value.([]any)
	if !ok || len(input) < 1 || len(input) > c.MaxMessages {
		return nil, errors.New("message count")
	}
	output := make([]map[string]string, 0, len(input))
	for _, item := range input {
		message, ok := item.(map[string]any)
		if !ok || only(message, "role", "content") != nil {
			return nil, errors.New("invalid message")
		}
		role, ok := message["role"].(string)
		if !ok || (role != "system" && role != "user" && role != "assistant") {
			return nil, errors.New("invalid role")
		}
		content, err := text(message, "content", true, budget)
		if err != nil {
			return nil, err
		}
		output = append(output, map[string]string{"role": role, "content": content})
	}
	return output, nil
}
func sampling(input, output map[string]any, budget *int) error {
	if value, exists := input["temperature"]; exists {
		parsed, err := decimal(value, 0, 2)
		if err != nil {
			return err
		}
		output["temperature"] = parsed
	}
	if value, exists := input["top_p"]; exists {
		parsed, err := decimal(value, 0, 1)
		if err != nil {
			return err
		}
		output["top_p"] = parsed
	}
	if value, exists := input["seed"]; exists {
		parsed, err := integer(value, -1, 2147483647)
		if err != nil {
			return err
		}
		output["seed"] = parsed
	}
	if value, exists := input["stop"]; exists {
		stops := []string{}
		switch item := value.(type) {
		case string:
			stops = append(stops, item)
		case []any:
			for _, entry := range item {
				str, ok := entry.(string)
				if !ok {
					return errors.New("invalid stop")
				}
				stops = append(stops, str)
			}
		default:
			return errors.New("invalid stop")
		}
		if len(stops) < 1 || len(stops) > 4 {
			return errors.New("stop count")
		}
		for _, stop := range stops {
			if len(stop) < 1 || len(stop) > 128 {
				return errors.New("stop length")
			}
			*budget -= len(stop)
			if *budget < 0 {
				return errInputLimit
			}
		}
		output["stop"] = stops
	}
	return nil
}

func normalize(raw []byte, route string, c Config) ([]byte, error) {
	input, err := parseObject(raw)
	if err != nil {
		return nil, err
	}
	model, ok := input["model"].(string)
	if !ok || model != c.Model {
		return nil, errors.New("model not allowed")
	}
	native := route == "/api/chat" || route == "/api/generate"
	stream, err := booleanField(input, "stream", native)
	if err != nil {
		return nil, err
	}
	output := map[string]any{"model": c.Model, "stream": stream}
	budget := c.InputBytes
	if native {
		allowed := []string{"model", "stream", "think", "options"}
		if route == "/api/chat" {
			allowed = append(allowed, "messages")
		} else {
			allowed = append(allowed, "prompt", "system")
		}
		if err = only(input, allowed...); err != nil {
			return nil, err
		}
		think, err := booleanField(input, "think", false)
		if err != nil || think {
			return nil, errors.New("thinking override not allowed")
		}
		output["think"] = false
		output["keep_alive"] = "5m"
		options := map[string]any{}
		if value, exists := input["options"]; exists {
			var ok bool
			options, ok = value.(map[string]any)
			if !ok {
				return nil, errors.New("options object required")
			}
		}
		if err = only(options, "num_ctx", "num_predict", "temperature", "top_p", "seed", "stop"); err != nil {
			return nil, err
		}
		normalized := map[string]any{"num_ctx": c.ContextTokens, "num_predict": c.OutputTokens}
		if value, exists := options["num_ctx"]; exists {
			if _, err = integer(value, 1, int64(c.ContextTokens)); err != nil {
				return nil, err
			}
		}
		if value, exists := options["num_predict"]; exists {
			parsed, err := integer(value, 1, int64(c.OutputTokens))
			if err != nil {
				return nil, err
			}
			normalized["num_predict"] = parsed
		}
		if err = sampling(options, normalized, &budget); err != nil {
			return nil, err
		}
		output["options"] = normalized
	} else {
		allowed := []string{"model", "stream", "max_tokens", "max_completion_tokens", "temperature", "top_p", "seed", "stop", "n", "stream_options"}
		if route == "/v1/chat/completions" {
			allowed = append(allowed, "reasoning_effort")
		}
		if route == "/v1/chat/completions" {
			allowed = append(allowed, "messages")
		} else if route == "/v1/completions" {
			allowed = append(allowed, "prompt")
		} else {
			return nil, errors.New("route not allowed")
		}
		if err = only(input, allowed...); err != nil {
			return nil, err
		}
		if route == "/v1/chat/completions" {
			if value, exists := input["reasoning_effort"]; exists && value != "none" {
				return nil, errors.New("reasoning override not allowed")
			}
			output["reasoning_effort"] = "none"
		}
		if value, exists := input["stream_options"]; exists {
			options, ok := value.(map[string]any)
			if !ok || !stream || only(options, "include_usage") != nil || len(options) != 1 {
				return nil, errors.New("invalid stream options")
			}
			usage, ok := options["include_usage"].(bool)
			if !ok {
				return nil, errors.New("invalid stream usage")
			}
			output["stream_options"] = map[string]bool{"include_usage": usage}
		}
		if value, exists := input["n"]; exists {
			if _, err = integer(value, 1, 1); err != nil {
				return nil, err
			}
		}
		output["n"] = 1
		output["max_tokens"] = c.OutputTokens
		first, hasFirst := input["max_tokens"]
		second, hasSecond := input["max_completion_tokens"]
		if hasFirst && hasSecond {
			return nil, errors.New("ambiguous token limit")
		}
		if hasSecond {
			first = second
		}
		if hasFirst || hasSecond {
			parsed, err := integer(first, 1, int64(c.OutputTokens))
			if err != nil {
				return nil, err
			}
			output["max_tokens"] = parsed
		}
		if err = sampling(input, output, &budget); err != nil {
			return nil, err
		}
	}
	if route == "/api/chat" || route == "/v1/chat/completions" {
		value, err := messages(input["messages"], c, &budget)
		if err != nil {
			return nil, err
		}
		output["messages"] = value
	} else {
		prompt, err := text(input, "prompt", true, &budget)
		if err != nil {
			return nil, err
		}
		output["prompt"] = prompt
		if route == "/api/generate" {
			if _, exists := input["system"]; exists {
				system, err := text(input, "system", false, &budget)
				if err != nil {
					return nil, err
				}
				output["system"] = system
			}
		}
	}
	return json.Marshal(output)
}
