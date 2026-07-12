#ifndef _LINUX_EXTENDED_SYSCALLS_H
#define _LINUX_EXTENDED_SYSCALLS_H

asmlinkage long sys_reset_swap_stats(void);
asmlinkage long sys_get_swap_stats(int __user *on_demand_swapin_num,
				   int __user *prefetch_swapin_num,
				   int __user *hiton_swap_cache_num);

asmlinkage long sys_rdtsc_start(void);
asmlinkage long sys_rdtsc_end(void);

#endif // _LINUX_EXTENDED_SYSCALLS_H