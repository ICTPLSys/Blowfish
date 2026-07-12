#include <linux/swap_stats.h>
#include <linux/syscalls.h>
#include <linux/printk.h>
#include <linux/hermit.h>

// time utils
// reference cycles.
// #1, Fix the clock cycles of CPU.
// #2, Divided by CPU frequency to calculate the wall time.
// 500 cycles/ 4.0GHz * 10^9 ns = 500/4.0 ns = xx ns.
// Use "__asm__" in header files (".h") and "asm" in source files (".c")
static inline uint64_t get_cycles_start_lfence(void)
{
	uint32_t cycles_high, cycles_low;
	__asm__ __volatile__("xorl %%eax, %%eax\n\t"
			     "LFENCE\n\t"
			     "RDTSC\n\t"
			     "mov %%edx, %0\n\t"
			     "mov %%eax, %1\n\t"
			     : "=r"(cycles_high), "=r"(cycles_low)::"%rax",
			       "%rbx", "%rcx", "%rdx");
	return ((uint64_t)cycles_high << 32) + (uint64_t)cycles_low;
}

// More strict than get_cycles_start since "RDTSCP; read registers; CPUID"
// gurantee all instructions before are executed and all instructions after
// are not speculativly executed
// Refer to https://www.intel.com/content/dam/www/public/us/en/documents/white-papers/ia-32-ia-64-benchmark-code-execution-paper.pdf
static inline uint64_t get_cycles_end_lfence(void)
{
	uint32_t cycles_high, cycles_low;
	__asm__ __volatile__("RDTSCP\n\t"
			     "mov %%edx, %0\n\t"
			     "mov %%eax, %1\n\t"
			     "xorl %%eax, %%eax\n\t"
			     "LFENCE\n\t"
			     : "=r"(cycles_high), "=r"(cycles_low)::"%rax",
			       "%rbx", "%rcx", "%rdx");
	return ((uint64_t)cycles_high << 32) + (uint64_t)cycles_low;
}


SYSCALL_DEFINE0(reset_swap_stats)
{
	reset_adc_swap_stats();
	reset_adc_pf_breakdown();
	return 0;
}

SYSCALL_DEFINE3(get_swap_stats, int __user *, ondemand_swapin_num, int __user *,
		prefetch_swapin_num, int __user *, hit_on_prefetch_num)
{
	int dmd_swapin_num;
	int prf_swapin_num;
	int hit_prftch_num;
	int swapout_num;

	report_adc_time_stat();
	report_adc_counters();
	report_adc_pf_breakdown(NULL);

	dmd_swapin_num = get_adc_counter(ADC_ONDEMAND_SWAPIN);
	prf_swapin_num = get_adc_counter(ADC_PREFETCH_SWAPIN);
	hit_prftch_num = get_adc_counter(ADC_HIT_ON_PREFETCH);
	swapout_num = get_adc_counter(ADC_SWAPOUT);

	put_user(dmd_swapin_num, ondemand_swapin_num);
	put_user(prf_swapin_num, prefetch_swapin_num);
	put_user(hit_prftch_num, hit_on_prefetch_num);

	return 0;
}

SYSCALL_DEFINE0(rdtsc_start)
{
	return get_cycles_start_lfence();
}

SYSCALL_DEFINE0(rdtsc_end)
{
	return get_cycles_end_lfence();
}
