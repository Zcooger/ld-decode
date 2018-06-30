#include <stdio.h>
#include <unistd.h>
#include <stdint.h>

// in number of samples - must be divisible by 3
#define BUFSIZE 4096*3 
	
int16_t inbuf[BUFSIZE];
uint32_t outbuf[BUFSIZE / 3];

inline uint16_t sconv(int16_t sample) 
{
	return (((unsigned int)sample) >> 6) & 0x3ff;
}

int main(void)
{
	size_t rv;
	int i;

	while ((rv = read(0, inbuf, sizeof(inbuf))) > 0) {
		for (i = 0; i < (rv / (3 * sizeof(int16_t))); i++) {
			outbuf[i] = sconv(inbuf[(i * 3)]) << 0;
			outbuf[i] |= sconv(inbuf[(i * 3) + 1]) << 10;
			outbuf[i] |= sconv(inbuf[(i * 3) + 2]) << 20;
		}
		rv = write(1, outbuf, i * sizeof(uint32_t));
	}
}

