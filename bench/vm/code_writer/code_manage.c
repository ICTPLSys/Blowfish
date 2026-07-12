#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <getopt.h>
#include <string.h>

#define STATUS_FILE "write_status"
#define TIME_FILE "write_time" 
#define STATUS_ROLL 3
#define STATUS_READY 2
#define STATUS_DONE 1
#define STATUS_EXIT 0

/* Read status file. */
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

/* Write status file. */
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

long long read_time() {
    FILE *fp = fopen(TIME_FILE, "r");
    if (fp == NULL) {
        fprintf(stderr, "Failed to open time file\n");
        return -1;
    }
    
    long long time_ns;
    if (fscanf(fp, "%lld", &time_ns) != 1) {
        fprintf(stderr, "Failed to read time\n");
        fclose(fp);
        return -1;
    }
    fclose(fp);
    return time_ns;
}

int main(int argc, char *argv[]) {
    int opt;
    enum {MODE_NONE, MODE_WAIT, MODE_CONTINUE, MODE_ROLL, MODE_EXIT, MODE_TIME} mode = MODE_NONE;
    
    /* Parse command-line options. */
    while ((opt = getopt(argc, argv, "wcetr")) != -1) {
        switch (opt) {
            case 'w':
                mode = MODE_WAIT;
                break;
            case 'c':
                mode = MODE_CONTINUE;
                break;
            case 'r':
                mode = MODE_ROLL;
                break;
            case 'e':
                mode = MODE_EXIT;
                break;
            case 't':
                mode = MODE_TIME;
                break;
            default:
                fprintf(stderr, "Usage: %s [-w|-c|-e|-t|-r]\n", argv[0]);
                fprintf(stderr, "  -w: wait for completion\n");
                fprintf(stderr, "  -c: continue after completion\n");
                fprintf(stderr, "  -e: exit after completion\n");
                fprintf(stderr, "  -t: read execution time\n");
                fprintf(stderr, "  -r: roll back after completion\n");
                exit(EXIT_FAILURE);
        }
    }
    
    if (mode == MODE_NONE) {
        fprintf(stderr, "Must specify one mode: -w, -c, -e, or -t\n");
        exit(EXIT_FAILURE);
    }
    
    /* Poll status until terminal state. */
    while (1) {
        int status = read_status();
        long long time_ns = 0;
        if (status == STATUS_ROLL && mode == MODE_CONTINUE) {
            write_status(STATUS_READY);
            printf("ROLLING, Sent stop signal.\n");
            break;
        }
        else if (status == STATUS_DONE) {
            switch (mode) {
                case MODE_WAIT:
                    printf("Operation completed.\n");
                    break;
                    
                case MODE_CONTINUE:
                    write_status(STATUS_READY);
                    printf("Operation completed. Sent continue signal.\n");
                    break;

                case MODE_ROLL:
                    write_status(STATUS_ROLL);
                    printf("Operation completed. Sent ROLLING signal.\n");
                    break;
                    
                case MODE_EXIT:
                    write_status(STATUS_EXIT);
                    printf("Operation completed. Sent exit signal.\n");
                    break;
                
                case MODE_TIME:
                    time_ns = read_time();
                    if (time_ns >= 0) {
                        printf("%lld\n", 
                               time_ns);
                    }
                    break;
                    
                default:
                    break;
            }
            break;
        }
        usleep(100000);  /* 100 ms throttle */
    }
    
    return 0;
}