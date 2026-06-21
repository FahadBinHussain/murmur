package cookies

import (
	"encoding/json"
	"os"

	"go.mau.fi/mautrix-meta/pkg/messagix/cookies"
	"go.mau.fi/mautrix-meta/pkg/messagix/types"
)

type CookieMap map[string]string

func LoadFromFile(path string) (CookieMap, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}

	var raw []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	}
	if err := json.Unmarshal(data, &raw); err == nil && len(raw) > 0 {
		m := make(CookieMap, len(raw))
		for _, c := range raw {
			m[c.Name] = c.Value
		}
		return m, nil
	}

	var m CookieMap
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, err
	}
	return m, nil
}

func ToMessagix(m CookieMap, platform types.Platform) *cookies.Cookies {
	c := &cookies.Cookies{Platform: platform}
	typed := make(map[cookies.MetaCookieName]string, len(m))
	for k, v := range m {
		typed[cookies.MetaCookieName(k)] = v
	}
	c.UpdateValues(typed)
	return c
}

func GetMissing(c *cookies.Cookies) []string {
	missing := c.GetMissingCookieNames()
	out := make([]string, len(missing))
	for i, n := range missing {
		out[i] = string(n)
	}
	return out
}