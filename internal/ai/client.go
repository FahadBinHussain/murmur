package ai

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type Client struct {
	BaseURL      string
	DefaultChat  string
	DefaultImage string
	HTTPClient   *http.Client
}

func NewClient(baseURL, defaultChat, defaultImage string) *Client {
	if baseURL == "" {
		baseURL = "https://alchoholpad-litellm-huggingface-template.hf.space/v1"
	}
	if defaultChat == "" {
		defaultChat = "openrouter/google/gemma-4-31b-it:free"
	}
	if defaultImage == "" {
		defaultImage = "cloudflare/@cf/black-forest-labs/flux-1-schnell"
	}
	return &Client{
		BaseURL:      baseURL,
		DefaultChat:  defaultChat,
		DefaultImage: defaultImage,
		HTTPClient:   &http.Client{Timeout: 60 * time.Second},
	}
}

type chatRequest struct {
	Model     string        `json:"model"`
	Messages  []chatMessage `json:"messages"`
	MaxTokens int           `json:"max_tokens"`
}

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type chatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
}

type imageRequest struct {
	Model  string `json:"model"`
	Prompt string `json:"prompt"`
	N      int    `json:"n"`
}

type imageResponse struct {
	Data []struct {
		URL string `json:"url"`
	} `json:"data"`
}

type modelEntry struct {
	ID          string `json:"id"`
	Usable      bool   `json:"usable"`
	ImageUsable bool   `json:"image_usable"`
}

type modelCatalogResponse struct {
	Data []modelEntry `json:"data"`
}

type modelsResponse struct {
	Data []struct {
		ID string `json:"id"`
	} `json:"data"`
}

func (c *Client) request(method, path string, body interface{}) (map[string]interface{}, error) {
	var bodyReader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		bodyReader = strings.NewReader(string(data))
	}

	req, err := http.NewRequest(method, c.BaseURL+path, bodyReader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.HTTPClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("HTTP %d: read error: %w", resp.StatusCode, err)
	}

	var result map[string]interface{}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("HTTP %d: decode error (body: %.200s)", resp.StatusCode, string(respBody))
	}
	return result, nil
}

func (c *Client) Chat(prompt string) (string, error) {
	return c.ChatWithModel(prompt, c.DefaultChat)
}

func (c *Client) ChatWithModel(prompt string, model string) (string, error) {
	if model == "" {
		model = c.DefaultChat
	}
	result, err := c.request("POST", "/chat/completions", chatRequest{
		Model:     model,
		Messages:  []chatMessage{{Role: "user", Content: prompt}},
		MaxTokens: 1024,
	})
	if err != nil {
		return "", err
	}
	if e, ok := result["error"]; ok {
		return "", fmt.Errorf("%v", e)
	}
	if choices, ok := result["choices"].([]interface{}); ok && len(choices) > 0 {
		if choice, ok := choices[0].(map[string]interface{}); ok {
			if msg, ok := choice["message"].(map[string]interface{}); ok {
				if content, ok := msg["content"].(string); ok {
					return content, nil
				}
			}
		}
	}
	return "no response from model", nil
}

func (c *Client) ImageRaw(prompt string) ([]byte, string, error) {
	return c.ImageRawWithModel(prompt, c.DefaultImage)
}

func (c *Client) ImageRawWithModel(prompt string, model string) ([]byte, string, error) {
	if prompt == "" {
		prompt = "a cute cat"
	}
	if model == "" {
		model = c.DefaultImage
	}
	result, err := c.request("POST", "/images/generations", imageRequest{
		Model:  model,
		Prompt: prompt,
		N:      1,
	})
	if err != nil {
		return nil, "", err
	}
	if e, ok := result["error"]; ok {
		return nil, "", fmt.Errorf("%v", e)
	}
	if data, ok := result["data"].([]interface{}); ok && len(data) > 0 {
		if item, ok := data[0].(map[string]interface{}); ok {
			if b64, ok := item["b64_json"].(string); ok && b64 != "" {
				decoded, err := base64.StdEncoding.DecodeString(b64)
				if err != nil {
					return nil, "", fmt.Errorf("base64 decode: %w", err)
				}
				return decoded, "image/png", nil
			}
			if url, ok := item["url"].(string); ok && url != "" {
				return nil, url, nil
			}
		}
	}
	if url, ok := result["url"].(string); ok && url != "" {
		return nil, url, nil
	}
	return nil, "", fmt.Errorf("no image data in response: %v", result)
}

func (c *Client) Image(prompt string) (string, error) {
	data, url, err := c.ImageRaw(prompt)
	if err != nil {
		return "", err
	}
	if url != "" {
		return fmt.Sprintf("[%s]\n%s", c.DefaultImage, url), nil
	}
	return fmt.Sprintf("[%s]\nImage generated (%d bytes)", c.DefaultImage, len(data)), nil
}

func (c *Client) ListModels(page int) string {
	text, _ := c.ListModelsWithList(page)
	return text
}

func (c *Client) ListModelsWithList(page int) (string, []string) {
	result, err := c.request("GET", "/model-catalog", nil)
	if err != nil {
		return fmt.Sprintf("[error] %v", err), nil
	}
	var models []string
	if data, ok := result["data"].([]interface{}); ok {
		for _, m := range data {
			if entry, ok := m.(map[string]interface{}); ok {
				if usable, _ := entry["usable"].(bool); usable {
					if imgUsable, _ := entry["image_usable"].(bool); !imgUsable {
						if id, ok := entry["id"].(string); ok {
							models = append(models, id)
						}
					}
				}
			}
		}
	}
	return paginateWithList(models, page, "Chat Models", "models")
}

func (c *Client) ListImageModels(page int) string {
	text, _ := c.ListImageModelsWithList(page)
	return text
}

func (c *Client) ListImageModelsWithList(page int) (string, []string) {
	result, err := c.request("GET", "/model-catalog", nil)
	if err != nil {
		return fmt.Sprintf("[error] %v", err), nil
	}
	var models []string
	if data, ok := result["data"].([]interface{}); ok {
		for _, m := range data {
			if entry, ok := m.(map[string]interface{}); ok {
				if imgUsable, _ := entry["image_usable"].(bool); imgUsable {
					if id, ok := entry["id"].(string); ok {
						models = append(models, id)
					}
				}
			}
		}
	}
	return paginateWithList(models, page, "Image Models", "image models")
}

func (c *Client) ListAllModels(page int) string {
	result, err := c.request("GET", "/models", nil)
	if err != nil {
		return fmt.Sprintf("[error] %v", err)
	}
	var models []string
	if data, ok := result["data"].([]interface{}); ok {
		for _, m := range data {
			if entry, ok := m.(map[string]interface{}); ok {
				if id, ok := entry["id"].(string); ok {
					models = append(models, id)
				}
			}
		}
	}
	return paginate(models, page, "Models", "models full")
}

func (c *Client) ListAllImageModels(page int) string {
	result, err := c.request("GET", "/models", nil)
	if err != nil {
		return fmt.Sprintf("[error] %v", err)
	}
	var models []string
	if data, ok := result["data"].([]interface{}); ok {
		for _, m := range data {
			if entry, ok := m.(map[string]interface{}); ok {
				if id, ok := entry["id"].(string); ok {
					if strings.Contains(id, "image") || strings.Contains(id, "flux") ||
						strings.Contains(id, "sd-xl") || strings.Contains(id, "stable-diffusion") ||
						strings.Contains(id, "sdxl") || strings.Contains(id, "illustrious") ||
						strings.Contains(id, "illustrij") {
						models = append(models, id)
					}
				}
			}
		}
	}
	return paginate(models, page, "Image Models", "image models full")
}

func paginate(models []string, page int, header, cmd string) string {
	text, _ := paginateWithList(models, page, header, cmd)
	return text
}

func paginateWithList(models []string, page int, header, cmd string) (string, []string) {
	const pageSize = 25
	total := len(models)
	totalPages := (total + pageSize - 1) / pageSize
	if totalPages < 1 {
		totalPages = 1
	}
	if page < 1 {
		page = 1
	}
	if page > totalPages {
		page = totalPages
	}
	start := (page - 1) * pageSize
	end := start + pageSize
	if end > total {
		end = total
	}

	var lines []string
	lines = append(lines, fmt.Sprintf("%s (%d-%d of %d):", header, start+1, end, total))
	lines = append(lines, "", "[LiteLLM gateway]")
	for i := start; i < end; i++ {
		lines = append(lines, fmt.Sprintf("%d. %s", i+1, models[i]))
	}
	lines = append(lines, "", fmt.Sprintf("Page %d/%d", page, totalPages))
	if page > 1 {
		lines = append(lines, fmt.Sprintf("Previous: /ai %s %d", cmd, page-1))
	}
	if page < totalPages {
		lines = append(lines, fmt.Sprintf("Next: /ai %s %d", cmd, page+1))
	}
	return strings.Join(lines, "\n"), models[start:end]
}

func ParsePage(parts []string, cmdIdx int) int {
	if len(parts) > cmdIdx {
		if n, err := strconv.Atoi(parts[cmdIdx]); err == nil {
			return n
		}
	}
	return 1
}

func (c *Client) HandleCommand(text string) string {
	text = strings.TrimSpace(text)
	lower := strings.ToLower(text)
	parts := strings.Fields(text)

	if lower == "/ai" || lower == "/ai " {
		return c.Help()
	}

	if lower == "/ai help" {
		return c.Help()
	}

	if lower == "/ai status" {
		return c.Status()
	}

	if strings.HasPrefix(lower, "/ai image models full") {
		page := ParsePage(parts, 4)
		return c.ListAllImageModels(page)
	}
	if strings.HasPrefix(lower, "/ai image models") {
		page := ParsePage(parts, 3)
		return c.ListImageModels(page)
	}
	if strings.HasPrefix(lower, "/ai models full") {
		page := ParsePage(parts, 3)
		return c.ListAllModels(page)
	}
	if strings.HasPrefix(lower, "/ai models") {
		page := ParsePage(parts, 2)
		return c.ListModels(page)
	}

	if strings.HasPrefix(lower, "/ai image ") {
		prompt := strings.TrimSpace(text[len("/ai image "):])
		resp, err := c.Image(prompt)
		if err != nil {
			return fmt.Sprintf("[image error] %v", err)
		}
		return resp
	}

	prompt := strings.TrimSpace(text[len("/ai "):])
	if prompt == "" {
		return c.Help()
	}
	resp, err := c.Chat(prompt)
	if err != nil {
		return fmt.Sprintf("[chat error] %v", err)
	}
	return fmt.Sprintf("[%s]\n%s", c.DefaultChat, resp)
}

func (c *Client) Help() string {
	return `murmur
github.com/FahadBinHussain/murmur

chat:
  /ai <prompt>              talk to the ai

image:
  /ai image <prompt>        generate an image

models:
  /ai models                list chat models
  /ai models full           list all models
  /ai image models          list image models
  /ai image models full     list all image models

select:
  /ai model <number>        set chat model
  /ai image model <number>  set image model

info:
  /ai status                show current config
  /ai help                  show this help`
}

func (c *Client) Status() string {
	return fmt.Sprintf(`murmur

platform: messenger
chat:     %s
image:    %s
version:  1.0.0`,
		c.DefaultChat,
		c.DefaultImage,
	)
}
