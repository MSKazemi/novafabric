//go:build !race

package novasealbatchsigner

func init() { raceEnabled = false }
