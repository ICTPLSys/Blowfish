#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>
#include <getopt.h>
#include <time.h>
#include <sys/mman.h>
/* x86 SIMD intrinsics */
#include <xmmintrin.h>
#include <emmintrin.h>
#include <sched.h>

#include <fcntl.h>
#include <sys/stat.h>

#define STATUS_FILE "write_status"
#define TIME_FILE "write_time"
#define STATUS_ROLL 3
#define STATUS_READY 2
#define STATUS_DONE 1
#define STATUS_EXIT 0
#define PAGE_SIZE 4096

#define GB (1024UL * 1024 * 1024)

typedef struct {
    int thread_id;
    size_t size;        /* bytes this thread owns */
    char* memory;       /* region base */
    unsigned int seed;  /* per-thread RNG seed */
} ThreadArgs;

pthread_barrier_t barrier;
volatile int should_stop = 0;

void* write_memory(void* arg) {
    ThreadArgs* args = (ThreadArgs*)arg;
    size_t num_pages = (args->size + PAGE_SIZE - 1) / PAGE_SIZE;
    int *page_ptr = (int*)args->memory;
    
    pthread_barrier_wait(&barrier);  /* sync before writing */

    printf("Thread %d writing %.2f GB at address %p\n", 
        args->thread_id, (double)args->size / GB, args->memory);

    while (!should_stop) {
        for(size_t i = 0; i < num_pages; i++) {
            *page_ptr = i + 1;  /* store page index */
            page_ptr += PAGE_SIZE/sizeof(int);  /* next page */
        }
        _mm_mfence();
    }
    
    return NULL;
}

/* Status file helpers */
void write_status(int status) {
    FILE *fp = fopen(STATUS_FILE, "w");
    if (fp == NULL) {
        fprintf(stderr, "Failed to open status file\n");
        exit(EXIT_FAILURE);
    }
    fprintf(fp, "%d", status);
    fflush(fp);
    fclose(fp);
}

int read_status() {
    FILE *fp = fopen(STATUS_FILE, "r");
    if (fp == NULL) {
        fprintf(stderr, "Failed to open status file\n");
        exit(EXIT_FAILURE);
    }
    int status;
    if (fscanf(fp, "%d", &status) != 1) {
        fprintf(stderr, "Failed to read status\n");
        fclose(fp);
        exit(EXIT_FAILURE);
    }
    fclose(fp);
    return status;
}

void write_time(long long ns) {
    FILE *fp = fopen(TIME_FILE, "w");
    if (fp == NULL) {
        fprintf(stderr, "Failed to open time file\n");
        return;
    }
    fprintf(fp, "%lld", ns);
    fflush(fp);
    fclose(fp);
}

void touch_pages(char* mem, size_t size) {
    size_t num_pages = (size + PAGE_SIZE - 1) / PAGE_SIZE;
    int *page_ptr = (int*)mem;
    
    printf("Touching %zu pages...\n", num_pages);
    for(size_t i = 0; i < num_pages; i++) {
        *page_ptr = rand();  /* touch page */
        page_ptr += PAGE_SIZE/sizeof(int);
    }
    _mm_mfence();
}

void flush_cache(void* p, size_t size) {
    const char *ptr = (const char *)p;
    for (size_t i = 0; i < size; i += 64) {
        _mm_clflush(ptr + i);
    }
    _mm_mfence();
}

static long long get_ns_timestamp() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

size_t total_memory = 0;

size_t per_thread_size;
pthread_t* threads;
ThreadArgs* thread_args;

void shuffle_page_indices(size_t *indices, size_t count) {
    /* Fisher-Yates shuffle */
    for (size_t i = count - 1; i > 0; i--) {
        size_t j = rand() % (i + 1);
        /* swap */
        size_t temp = indices[i];
        indices[i] = indices[j];
        indices[j] = temp;
    }
}

#define RANDOM

int main(int argc, char *argv[]) {
    int num_threads = 0;
    int opt;

    /* CLI */
    while ((opt = getopt(argc, argv, "t:m:")) != -1) {
        switch (opt) {
            case 't':
                num_threads = atoi(optarg);
                break;
            case 'm':
                total_memory = (size_t)atol(optarg) * GB;
                break;
            default:
                fprintf(stderr, "Usage: %s -t thread_number -m memory_size(GB)\n", argv[0]);
                exit(EXIT_FAILURE);
        }
    }

    if (num_threads < 0 || total_memory <= 0) {
        fprintf(stderr, "Argument Wrong\n");
        exit(EXIT_FAILURE);
    }
#ifdef RANDOM
    srand(time(NULL));
    /* srand(0); */
#endif

    if (num_threads == 0) {
        char* total_memory_ptr = mmap(NULL, total_memory, 
                                    PROT_READ | PROT_WRITE,
                                    MAP_PRIVATE | MAP_ANONYMOUS,
                                    -1, 0);
        if (total_memory_ptr == MAP_FAILED) {
            total_memory_ptr = malloc(total_memory);
            if (total_memory_ptr == NULL) {
                fprintf(stderr, "Failed to allocate memory\n");
                exit(EXIT_FAILURE);
            }
        }

        write_status(STATUS_READY);
    
        memset(total_memory_ptr, 0, total_memory);
        touch_pages(total_memory_ptr, total_memory);

        printf("Single thread mode initiated in main thread\n");
        
        size_t num_pages = (total_memory + PAGE_SIZE - 1) / PAGE_SIZE;
        size_t *page_indices = malloc(num_pages * sizeof(size_t));
        int *base_ptr = (int*)total_memory_ptr;
        long long start_time;
        if (!page_indices) {
            fprintf(stderr, "Failed to allocate page indices array\n");
            exit(EXIT_FAILURE);
        }
        
        while (1) {
            for (size_t i = 0; i < num_pages; i++) {
                page_indices[i] = i;
            }
            
#ifdef RANDOM
            shuffle_page_indices(page_indices, num_pages);
#endif
            
            start_time = get_ns_timestamp();
                
            for(size_t i = 0; i < num_pages; i++) {
#ifdef RANDOM
                size_t page_idx = page_indices[i];
#else
                size_t page_idx = i;
#endif
                volatile int *volatile_ptr = (volatile int *)(base_ptr + page_idx * (PAGE_SIZE/sizeof(int)));
                *volatile_ptr = page_idx + 1;  
                asm volatile("" ::: "memory");
            }
            
            _mm_mfence();
            
            long long end_time = get_ns_timestamp();
            long long elapsed_ns = end_time - start_time;

            // int roll_count = 0;
            
            while(read_status() == STATUS_ROLL)
            {
                for(size_t i = 0; i < 3932160; i++) {   //15GB : 3932160
#ifdef RANDOM
                    size_t page_idx = page_indices[i];
#else
                    size_t page_idx = i;
#endif
                    volatile int *volatile_ptr = (volatile int *)(base_ptr + page_idx * (PAGE_SIZE/sizeof(int)));
                    *volatile_ptr = page_idx + 1;  
                    asm volatile("" ::: "memory");
                    _mm_mfence();
                    usleep(1000);

                    if(read_status() != STATUS_ROLL)
                        break;
                }
                
                // usleep(2000000);
                // roll_count++;
                // if(roll_count >= 60) {
                //     while(read_status() == STATUS_ROLL)
                //     {
                //         usleep(100000);
                //     }
                // }
            }

            write_time(elapsed_ns);
            write_status(STATUS_DONE);
            printf("Write completed in %lld ns\n", elapsed_ns);
            
            int status;
            while (1) {
                status = read_status();
                if (status == STATUS_READY || status == STATUS_ROLL) {
                    break;
                } else if (status == STATUS_EXIT) {
                    if (munmap(total_memory_ptr, total_memory) != 0) {
                        free(total_memory_ptr);
                    }
                    free(page_indices);
                    unlink(STATUS_FILE);
                    unlink(TIME_FILE);
                    return 0;
                }
                usleep(100000);
            }
        }
    }

    per_thread_size = total_memory / num_threads;

    threads = malloc(num_threads * sizeof(pthread_t));
    thread_args = malloc(num_threads * sizeof(ThreadArgs));
    
    if (!threads || !thread_args) {
        fprintf(stderr, "Failed to allocate thread arrays\n");
        exit(EXIT_FAILURE);
    }

    if (pthread_barrier_init(&barrier, NULL, num_threads) != 0) {
        fprintf(stderr, "Failed to initialize barrier\n");
        exit(EXIT_FAILURE);
    }

    write_status(STATUS_READY);

    char* total_memory_ptr = mmap(NULL, total_memory, 
                                 PROT_READ | PROT_WRITE,
                                 MAP_PRIVATE | MAP_ANONYMOUS,
                                 -1, 0);
    if (total_memory_ptr == MAP_FAILED) {
        total_memory_ptr = malloc(total_memory);
        if (total_memory_ptr == NULL) {
            fprintf(stderr, "Failed to allocate memory\n");
            exit(EXIT_FAILURE);
        }
    }

    memset(total_memory_ptr, 0, total_memory);
    // madvise(total_memory_ptr, total_memory, MADV_HUGEPAGE);
    // mlock(total_memory_ptr, total_memory);

    while (1) {
        should_stop = 0;
        printf("Starting threads...\n");

        for (int i = 0; i < num_threads; i++) {
            thread_args[i].thread_id = i;
            thread_args[i].size = per_thread_size;
            thread_args[i].memory = total_memory_ptr + (i * per_thread_size);
            if (pthread_create(&threads[i], NULL, write_memory, &thread_args[i]) != 0) {
                fprintf(stderr, "Thread creation failed\n");
                exit(EXIT_FAILURE);
            }
        }

        printf("All threads started, writing...\n");
        sleep(5);

        should_stop = 1;
        for (int i = 0; i < num_threads; i++) {
            pthread_join(threads[i], NULL);
        }

        printf("All threads finished writing.\n");
        write_status(STATUS_DONE);

        int status;
        while (1) {
            status = read_status();
            if (status == STATUS_READY) {
                break;
            } else if (status == STATUS_EXIT) {
                goto cleanup;
            }
            usleep(100000);  /* 100 ms throttle */
        }
    }
cleanup:
    unlink(STATUS_FILE);
    pthread_barrier_destroy(&barrier);
    if (total_memory_ptr != MAP_FAILED && total_memory_ptr != NULL) {
        if (munmap(total_memory_ptr, total_memory) != 0) {
            free(total_memory_ptr);  /* malloc fallback */
        }
    }
    free(threads);
    free(thread_args);

    return 0;
}