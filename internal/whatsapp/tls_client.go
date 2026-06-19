package whatsapp

import (
	"context"
	"crypto/tls"
	"net"
	"net/http"
	"time"

	utls "github.com/refraction-networking/utls"
)

func NewChromeHTTPClient(proxyAddr string) *http.Client {
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{
			InsecureSkipVerify: false,
		},
		DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			// Create uTLS connection that impersonates Chrome
			utlsConn, err := utls.Dial("tcp", addr, &utls.Config{
				InsecureSkipVerify: false,
				NextProtos:         []string{"h2", "http/1.1"},
			})
			if err != nil {
				return nil, err
			}
			return utlsConn, nil
		},
		MaxIdleConns:          100,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:  10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}

	return &http.Client{
		Transport: transport,
		Timeout:   30 * time.Second,
	}
}
