package whatsapp

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"time"

	utls "github.com/refraction-networking/utls"
)

func NewChromeHTTPClient(proxyAddr string) *http.Client {
	dialer := &net.Dialer{
		Timeout:   15 * time.Second,
		KeepAlive: 30 * time.Second,
	}

	transport := &http.Transport{
		DialTLSContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			// Parse host
			host, _, err := net.SplitHostPort(addr)
			if err != nil {
				host = addr
			}

			// Create raw TCP connection
			rawConn, err := dialer.DialContext(ctx, "tcp", addr)
			if err != nil {
				return nil, fmt.Errorf("tcp dial: %w", err)
			}

			// Create uTLS client with Chrome 120 fingerprint
			config := &utls.Config{
				InsecureSkipVerify: false,
				ServerName:         host,
			}
			uconn := utls.UClient(rawConn, config, utls.HelloChrome_120)

			// Set SNI
			uconn.SetSNI(host)

			// Perform TLS handshake
			err = uconn.Handshake()
			if err != nil {
				rawConn.Close()
				return nil, fmt.Errorf("utls handshake: %w", err)
			}

			return uconn.NetConn(), nil
		},
		DialContext:         (&net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns:        100,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 15 * time.Second,
	}

	return &http.Client{
		Transport: transport,
		Timeout:   30 * time.Second,
	}
}
