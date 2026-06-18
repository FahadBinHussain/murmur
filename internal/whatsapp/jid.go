package whatsapp

import "hash/fnv"

func JIDToThreadID(jid string) int64 {
	h := fnv.New64a()
	h.Write([]byte(jid))
	return int64(h.Sum64())
}
