/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_BLOWFISH_RECLAIM_H
#define _LINUX_BLOWFISH_RECLAIM_H

#include <linux/compiler.h>
#include <linux/types.h>

struct folio;
#ifdef CONFIG_BLOWFISH
bool blowfish_restore_reported_folio(unsigned long pfn);
#else
static inline bool blowfish_restore_reported_folio(unsigned long pfn)
{
	return false;
}
#endif

#ifdef CONFIG_BLOWFISH
extern bool blowfish_async_reclaim_registered;


extern bool blowfish_use_async_reclaim;
int blowfish_async_reclaim_try_enqueue(u64 gfn, u32 len);
#endif

#endif /* _LINUX_BLOWFISH_RECLAIM_H */
