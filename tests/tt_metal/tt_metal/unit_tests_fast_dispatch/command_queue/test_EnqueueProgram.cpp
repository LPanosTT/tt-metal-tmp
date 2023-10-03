// SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.
//
// SPDX-License-Identifier: Apache-2.0

#include "command_queue_fixture.hpp"
#include "command_queue_test_utils.hpp"
#include "gtest/gtest.h"
#include "tt_metal/common/bfloat16.hpp"
#include "tt_metal/host_api.hpp"
#include "tt_metal/detail/tt_metal.hpp"

using namespace tt::tt_metal;

struct CBConfig {
    u32 num_pages;
    u32 page_size;
    tt::DataFormat data_format;
};

struct DummyProgramConfig {
    CoreRangeSet cr_set;
    CBConfig cb_config;
    u32 num_cbs;
    u32 num_sems;
    u32 first_cb_start;
};

namespace local_test_functions {

void initialize_dummy_kernels(Program& program, const CoreRangeSet& cr_set) {
    auto dummy_reader_kernel = CreateDataMovementKernel(
        program, "tt_metal/kernels/dataflow/blank.cpp", cr_set,
        DataMovementConfig{.processor = DataMovementProcessor::RISCV_1, .noc = NOC::RISCV_1_default});

    auto dummy_writer_kernel = CreateDataMovementKernel(
        program, "tt_metal/kernels/dataflow/blank.cpp", cr_set,
        DataMovementConfig{.processor = DataMovementProcessor::RISCV_0, .noc = NOC::RISCV_0_default});

    auto dummy_compute_kernel = CreateComputeKernel(program, "tt_metal/kernels/compute/blank.cpp", cr_set);
}

bool test_dummy_EnqueueProgram_with_cbs(Device* device, CommandQueue& cq, const DummyProgramConfig& program_config) {
    bool pass = true;

    Program program;

    u32 cb_num_pages = program_config.cb_config.num_pages;
    u32 cb_size = program_config.cb_config.num_pages * program_config.cb_config.page_size;
    u32 cb_addr = program_config.first_cb_start;

    for (u32 cb_id = 0; cb_id < program_config.num_cbs; cb_id++) {
        CircularBufferConfig cb_config = CircularBufferConfig(cb_size, {{cb_id, program_config.cb_config.data_format}}, cb_addr).set_page_size(cb_id, program_config.cb_config.page_size);
        auto cb = CreateCircularBuffer(program, program_config.cr_set, cb_config);
        cb_addr += cb_size;
    }

    initialize_dummy_kernels(program, program_config.cr_set);
    EnqueueProgram(cq, program, false);
    Finish(cq);

    // Need to use old APIs to read since we cannot allocate a buffer in the reserved space we're trying
    // to read from
    vector<u32> cb_config_vector;
    u32 cb_config_buffer_size = program_config.num_cbs * UINT32_WORDS_PER_CIRCULAR_BUFFER_CONFIG * sizeof(u32);

    cb_addr = program_config.first_cb_start;
    for (const CoreRange& core_range : program_config.cr_set.ranges()) {
        CoresInCoreRangeGenerator core_range_generator(core_range, device->logical_grid_size());

        bool terminate;
        do {
            auto [core_coord, terminate_] = core_range_generator();

            terminate = terminate_;
            tt::tt_metal::detail::ReadFromDeviceL1(
                device, core_coord, CIRCULAR_BUFFER_CONFIG_BASE, cb_config_buffer_size, cb_config_vector);

            u32 cb_id = 0;
            for (u32 i = 0; i < cb_config_vector.size(); i += sizeof(u32)) {
                bool addr_match = cb_config_vector.at(i) == ((cb_addr + cb_size * cb_id) >> 4);
                cb_id++;
                bool size_match = cb_config_vector.at(i + 1) == (cb_size >> 4);
                bool num_pages_match = cb_config_vector.at(i + 2) == cb_num_pages;

                pass &= (addr_match and size_match and num_pages_match);
            }

        } while (not terminate);
    }

    return pass;
}

bool test_dummy_EnqueueProgram_with_sems(Device* device, CommandQueue& cq, const DummyProgramConfig& program_config) {
    bool pass = true;

    Program program;

    for (u32 sem_id = 0; sem_id < program_config.num_sems; sem_id++) {
        auto sem = CreateSemaphore(program, program_config.cr_set, sem_id);
    }

    EnqueueProgram(cq, program, false);
    Finish(cq);

    vector<u32> sem_vector;
    u32 sem_buffer_size = program_config.num_sems * SEMAPHORE_ALIGNMENT;

    for (const CoreRange& core_range : program_config.cr_set.ranges()) {
        CoresInCoreRangeGenerator core_range_generator(core_range, device->logical_grid_size());

        bool terminate;
        do {
            auto [core_coord, terminate_] = core_range_generator();

            terminate = terminate_;
            tt::tt_metal::detail::ReadFromDeviceL1(device, core_coord, SEMAPHORE_BASE, sem_buffer_size, sem_vector);

            u32 sem_id = 0;
            for (u32 i = 0; i < sem_vector.size(); i += sizeof(u32)) {
                bool sem_match = sem_vector.at(i) == sem_id;
                sem_id++;

                pass &= sem_match;
            }
        } while (not terminate);
    }

    return pass;
}

bool test_EnqueueWrap_on_EnqueueWriteBuffer(Device* device, CommandQueue& cq, const BufferConfig& config) {
    EnqueueWriteBuffer_prior_to_wrap(device, cq, config);

    /*
    This just ensures we don't hang on the subsequent EnqueueWriteBuffer
    */
    size_t buf_size = config.num_pages * config.page_size;
    Buffer buffer(device, buf_size, config.page_size, config.buftype);

    vector<u32> src(buf_size / sizeof(u32), 0);

    for (u32 i = 0; i < src.size(); i++) {
        src.at(i) = i;
    }
    EnqueueWriteBuffer(cq, buffer, src, false);
    Finish(cq);

    return true;
}

bool test_EnqueueWrap_on_Finish(Device* device, CommandQueue& cq, const BufferConfig& config) {
    bool pass = true;
    EnqueueWriteBuffer_prior_to_wrap(device, cq, config);

    return pass;
}

bool test_EnqueueWrap_on_EnqueueProgram(Device* device, CommandQueue& cq, const BufferConfig& config) {
    bool pass = true;
    EnqueueWriteBuffer_prior_to_wrap(device, cq, config);

    return pass;
}

}  // namespace local_test_functions

namespace basic_tests {

namespace compiler_workaround_hardware_bug_tests {

TEST_F(CommandQueueFixture, TestArbiterDoesNotHang) {
    Program program;

    CoreRange cr = {.start = {0, 0}, .end = {0, 0}};
    CoreRangeSet cr_set({cr});
    // Add an NCRISC blank manually, but in compile program, the BRISC blank will be
    // added separately
    auto dummy_reader_kernel = CreateDataMovementKernel(
        program, "tt_metal/kernels/dataflow/unit_tests/command_queue/arbiter_hang.cpp", cr_set, DataMovementConfig{.processor = DataMovementProcessor::RISCV_1, .noc = NOC::RISCV_1_default});

    EnqueueProgram(*::detail::GLOBAL_CQ, program, false);
    Finish(*::detail::GLOBAL_CQ);
}

}

namespace single_core_tests {

TEST_F(CommandQueueFixture, TestSingleCbConfigCorrectlySentSingleCore) {
    CoreRange cr = {.start = {0, 0}, .end = {0, 0}};
    CoreRangeSet cr_set({cr});

    CBConfig cb_config = {.num_pages = 1, .page_size = 2048, .data_format = tt::DataFormat::Float16_b};

    uint32_t cb_size = cb_config.num_pages * cb_config.page_size;
    uint32_t l1_buffer_size = 1 * cb_size;
    auto l1_buffer = CreateBuffer(this->device_, l1_buffer_size, l1_buffer_size, BufferType::L1);

    DummyProgramConfig config = {.cr_set = cr_set, .cb_config = cb_config, .num_cbs = 1, .first_cb_start = l1_buffer.address()};

    EXPECT_TRUE(local_test_functions::test_dummy_EnqueueProgram_with_cbs(this->device_, *tt::tt_metal::detail::GLOBAL_CQ, config));
}

TEST_F(CommandQueueFixture, TestSingleSemaphoreConfigCorrectlySentSingleCore) {
    CoreRange cr = {.start = {0, 0}, .end = {0, 0}};
    CoreRangeSet cr_set({cr});

    DummyProgramConfig config = {.cr_set = cr_set, .num_sems = 1};

    EXPECT_TRUE(local_test_functions::test_dummy_EnqueueProgram_with_sems(this->device_, *tt::tt_metal::detail::GLOBAL_CQ, config));
}

TEST_F(CommandQueueFixture, TestAutoInsertedBlankBriscKernelInDeviceDispatchMode) {
    Program program;

    CoreRange cr = {.start = {0, 0}, .end = {0, 0}};
    CoreRangeSet cr_set({cr});
    // Add an NCRISC blank manually, but in compile program, the BRISC blank will be
    // added separately
    auto dummy_reader_kernel = CreateDataMovementKernel(
        program, "tt_metal/kernels/dataflow/blank.cpp", cr_set,
        DataMovementConfig{.processor = DataMovementProcessor::RISCV_1, .noc = NOC::RISCV_1_default});

    EnqueueProgram(*tt::tt_metal::detail::GLOBAL_CQ, program, false);
    Finish(*tt::tt_metal::detail::GLOBAL_CQ);
}

TEST_F(CommandQueueFixture, ComputeRuntimeArgs) {

    Program program;

    CoreRange cr = {.start = {0, 0}, .end = {0, 0}};
    CoreRangeSet cr_set({cr});

    auto compute_kernel_id = CreateComputeKernel(
        program,
        "tests/tt_metal/tt_metal/test_kernels/increment_runtime_arg.cpp",
        cr_set,
        tt::tt_metal::ComputeConfig{});


    std::vector<uint32_t> initial_runtime_args = {101, 202};
    SetRuntimeArgs(program, program.kernel_ids().at(0), cr_set, initial_runtime_args);
    EnqueueProgram(*tt::tt_metal::detail::GLOBAL_CQ, program, false);
    Finish(*tt::tt_metal::detail::GLOBAL_CQ);

    std::vector<uint32_t> increments = {87, 216};
    std::vector<uint32_t> written_args;
    CoreCoord logical_core(0,0);
    tt::tt_metal::detail::ReadFromDeviceL1(
        this->device_, logical_core, TRISC_L1_ARG_BASE, initial_runtime_args.size() * sizeof(uint32_t), written_args);
    for(int i=0; i<initial_runtime_args.size(); i++){
        bool got_expected_result = (written_args[i] == (initial_runtime_args[i] + increments[i]));
        EXPECT_TRUE(got_expected_result);
    }
}

}  // end namespace single_core_tests

namespace multicore_tests {
TEST_F(CommandQueueFixture, TestAllCbConfigsCorrectlySentMultiCore) {
    CoreCoord worker_grid_size = this->device_->logical_grid_size();

    CoreRange cr = {.start = {0, 0}, .end = {worker_grid_size.x - 1, worker_grid_size.y - 2}};
    CoreRangeSet cr_set({cr});

    CBConfig cb_config = {.num_pages = 1, .page_size = 2048, .data_format = tt::DataFormat::Float16_b};

    uint32_t cb_size = cb_config.num_pages * cb_config.page_size;
    uint32_t l1_buffer_size = NUM_CIRCULAR_BUFFERS * cb_size;
    auto l1_buffer = CreateBuffer(this->device_, l1_buffer_size, l1_buffer_size, BufferType::L1);

    DummyProgramConfig config = {
        .cr_set = cr_set, .cb_config = cb_config, .num_cbs = NUM_CIRCULAR_BUFFERS, .first_cb_start = l1_buffer.address()};

    EXPECT_TRUE(local_test_functions::test_dummy_EnqueueProgram_with_cbs(this->device_, *tt::tt_metal::detail::GLOBAL_CQ, config));
}

TEST_F(CommandQueueFixture, TestAllSemConfigsCorrectlySentMultiCore) {
    CoreCoord worker_grid_size = this->device_->logical_grid_size();

    CoreRange cr = {.start = {0, 0}, .end = {worker_grid_size.x - 1, worker_grid_size.y - 2}};
    CoreRangeSet cr_set({cr});

    DummyProgramConfig config = {.cr_set = cr_set, .num_sems = NUM_SEMAPHORES};

    EXPECT_TRUE(local_test_functions::test_dummy_EnqueueProgram_with_sems(this->device_, *tt::tt_metal::detail::GLOBAL_CQ, config));
}

}  // end namespace multicore_tests

namespace dram_cache_tests {
TEST_F(CommandQueueFixture, DISABLED_TestDramCacheHit) {}

TEST_F(CommandQueueFixture, DISABLED_TestDramCacheMatch) {}

TEST_F(CommandQueueFixture, DISABLED_TestProgramVectorSizeMatch) {}

}  // end namespace dram_cache_tests
}  // end namespace basic_tests

namespace stress_tests {
TEST_F(CommandQueueFixture, DISABLED_TestSendMaxNumberOfRuntimeArgs) {}

}  // namespace stress_tests
